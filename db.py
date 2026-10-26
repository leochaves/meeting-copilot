"""Banco local (SQLite) — reuniões, falas, explicações e configurações.

Fica em dados/copiloto.db; nada sai do computador.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

PASTA_DADOS = Path(__file__).parent / "dados"

ESQUEMA = """
create table if not exists reunioes (
    id       integer primary key,
    titulo   text not null,
    inicio   real not null,
    fim      real,
    duracao  real default 0,
    status   text not null,          -- gravando | pausada | gerando_ata | encerrada | interrompida
    ata      text,
    custo    real default 0,
    cerebro  text,
    assunto  text default ''
);
create table if not exists falas (
    id          integer primary key,
    reuniao_id  integer not null references reunioes(id) on delete cascade,
    t           real not null,       -- segundos desde o início da reunião
    quem        text not null,
    texto       text not null
);
create table if not exists explicacoes (
    id          integer primary key,
    reuniao_id  integer not null references reunioes(id) on delete cascade,
    t           real not null,
    tipo        text not null,       -- explicacao | pergunta
    pergunta    text,
    texto       text not null,
    segundos    real,
    custo       real default 0
);
create table if not exists revisoes (
    id          integer primary key,
    reuniao_id  integer not null references reunioes(id) on delete cascade,
    papel       text not null,       -- usuario | ia
    texto       text not null,
    dados       text,                -- JSON com as propostas da IA
    criado      real not null
);
create table if not exists config (
    chave  text primary key,
    valor  text not null
);
create index if not exists falas_reuniao on falas(reuniao_id);
create index if not exists explicacoes_reuniao on explicacoes(reuniao_id);
"""


class Db:
    def __init__(self, arquivo: Path = PASTA_DADOS / "copiloto.db"):
        arquivo.parent.mkdir(exist_ok=True)
        self._con = sqlite3.connect(arquivo, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._con.execute("pragma foreign_keys = on")
            self._con.executescript(ESQUEMA)
            # Colunas novas em bancos criados por versões anteriores
            colunas = {r[1] for r in self._con.execute("pragma table_info(reunioes)")}
            if "assunto" not in colunas:
                self._con.execute("alter table reunioes add column assunto text default ''")
            # Reuniões que estavam abertas quando o app fechou/caiu
            self._con.execute(
                "update reunioes set status = 'interrompida', fim = coalesce(fim, inicio + duracao) "
                "where status in ('gravando', 'pausada', 'gerando_ata')"
            )
            self._con.commit()

    def _exec(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._con.execute(sql, args)
            self._con.commit()
            return cur

    def _todos(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, args).fetchall()]

    # --- reuniões ----------------------------------------------------------

    def criar_reuniao(self, titulo: str, cerebro: str, assunto: str = "") -> int:
        cur = self._exec(
            "insert into reunioes (titulo, inicio, status, cerebro, assunto) values (?, ?, 'gravando', ?, ?)",
            (titulo, time.time(), cerebro, assunto),
        )
        return cur.lastrowid

    def atualizar_reuniao(self, rid: int, **campos):
        if not campos:
            return
        sets = ", ".join(f"{k} = ?" for k in campos)
        self._exec(f"update reunioes set {sets} where id = ?", (*campos.values(), rid))

    def somar_custo(self, rid: int, valor: float):
        if valor:
            self._exec("update reunioes set custo = custo + ? where id = ?", (valor, rid))

    def listar_reunioes(self, busca: str = "") -> list[dict]:
        sql = """
            select r.id, r.titulo, r.inicio, r.fim, r.duracao, r.status, r.custo, r.cerebro,
                   (select count(*) from falas f where f.reuniao_id = r.id) as n_falas,
                   r.ata is not null as tem_ata
            from reunioes r
        """
        args: tuple = ()
        if busca:
            sql += """ where r.titulo like ? or r.ata like ?
                       or exists (select 1 from falas f where f.reuniao_id = r.id and f.texto like ?)"""
            termo = f"%{busca}%"
            args = (termo, termo, termo)
        return self._todos(sql + " order by r.inicio desc", args)

    def obter_reuniao(self, rid: int) -> dict | None:
        r = self._todos("select * from reunioes where id = ?", (rid,))
        if not r:
            return None
        reuniao = r[0]
        reuniao["falas"] = self._todos(
            "select t, quem, texto from falas where reuniao_id = ? order by t, id", (rid,)
        )
        reuniao["explicacoes"] = self._todos(
            "select t, tipo, pergunta, texto, segundos, custo from explicacoes "
            "where reuniao_id = ? order by id",
            (rid,),
        )
        return reuniao

    def apagar_reuniao(self, rid: int):
        self._exec("delete from reunioes where id = ?", (rid,))

    def custo_mes(self) -> float:
        lt = time.localtime()
        inicio_mes = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        r = self._todos("select coalesce(sum(custo), 0) as c from reunioes where inicio >= ?", (inicio_mes,))
        return r[0]["c"]

    # --- falas / explicações -----------------------------------------------

    def adicionar_fala(self, rid: int, t: float, quem: str, texto: str):
        self._exec(
            "insert into falas (reuniao_id, t, quem, texto) values (?, ?, ?, ?)", (rid, t, quem, texto)
        )

    def adicionar_explicacao(self, rid: int, t: float, tipo: str, pergunta: str | None,
                             texto: str, segundos: float, custo: float):
        self._exec(
            "insert into explicacoes (reuniao_id, t, tipo, pergunta, texto, segundos, custo) "
            "values (?, ?, ?, ?, ?, ?, ?)",
            (rid, t, tipo, pergunta, texto, segundos, custo),
        )

    # --- revisão pós-reunião ------------------------------------------------

    def listar_revisao(self, rid: int) -> list[dict]:
        linhas = self._todos(
            "select id, papel, texto, dados, criado from revisoes where reuniao_id = ? order by id", (rid,)
        )
        for l in linhas:
            l["dados"] = json.loads(l["dados"]) if l["dados"] else None
        return linhas

    def adicionar_revisao(self, rid: int, papel: str, texto: str, dados: dict | None = None):
        self._exec(
            "insert into revisoes (reuniao_id, papel, texto, dados, criado) values (?, ?, ?, ?, ?)",
            (rid, papel, texto, json.dumps(dados, ensure_ascii=False) if dados else None, time.time()),
        )

    def substituir_textos(self, rid: int, trocar) -> int:
        """Aplica `trocar(texto) -> texto` nas falas e explicações da reunião;
        devolve quantos textos mudaram."""
        mudou = 0
        with self._lock:
            for tabela in ("falas", "explicacoes"):
                for linha in self._con.execute(f"select id, texto from {tabela} where reuniao_id = ?", (rid,)).fetchall():
                    novo = trocar(linha["texto"])
                    if novo != linha["texto"]:
                        self._con.execute(f"update {tabela} set texto = ? where id = ?", (novo, linha["id"]))
                        mudou += 1
            self._con.commit()
        return mudou

    # --- configuração ------------------------------------------------------

    def ler_config(self) -> dict:
        return {r["chave"]: json.loads(r["valor"]) for r in self._todos("select * from config")}

    def salvar_config(self, valores: dict):
        with self._lock:
            self._con.executemany(
                "insert into config (chave, valor) values (?, ?) "
                "on conflict(chave) do update set valor = excluded.valor",
                [(k, json.dumps(v)) for k, v in valores.items()],
            )
            self._con.commit()
