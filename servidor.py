"""API local (FastAPI) que a interface usa: comandos via HTTP, eventos ao vivo via WebSocket."""

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import monitor
import revisao
from audio import listar_dispositivos
from cerebro_local import listar_modelos
from db import PASTA_DADOS, Db
from sessao import Sessao, criar_cerebro, gerar_ata, mmss

RAIZ = Path(__file__).parent
load_dotenv(RAIZ / ".env")

PADRAO = {
    "cerebro": "local",  # claude | local
    "modelo_claude": os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
    "esforco": os.getenv("CLAUDE_EFFORT", "low"),
    "modelo_local": os.getenv("OLLAMA_MODEL", "gemma3:4b"),
    "transcricao": os.getenv("TRANSCRICAO", "whisper"),  # whisper | deepgram
    "whisper_modelo": os.getenv("WHISPER_MODEL", "large-v3-turbo"),
    "whisper_dispositivo": "gpu",  # gpu | cpu
    "local_dispositivo": "gpu",  # gpu | cpu
    "limiar": float(os.getenv("WHISPER_LIMIAR", "250")),
    "intervalo": 60,
    "usar_mic": True,
    "mic_index": None,  # None = padrão do Windows
    "saida_index": None,
    # Vocabulário e contexto (ajudam a transcrição e o modelo a acertar nomes)
    "contexto": "",
    "vocabulario": "",
    "correcoes": "",
    "anthropic_key": os.getenv("ANTHROPIC_API_KEY", ""),
    "deepgram_key": os.getenv("DEEPGRAM_API_KEY", ""),
}
CHAVES_SECRETAS = ("anthropic_key", "deepgram_key")

db = Db()
sessao: Sessao | None = None


def config() -> dict:
    return {**PADRAO, **db.ler_config()}


# --- eventos ao vivo ---------------------------------------------------------


class Hub:
    """Repassa eventos das threads da sessão pros WebSockets abertos."""

    def __init__(self):
        self.clientes: set[asyncio.Queue] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def emitir(self, evento: dict):
        if not self.loop:
            return
        msg = json.dumps(evento, ensure_ascii=False)
        for fila in list(self.clientes):
            self.loop.call_soon_threadsafe(fila.put_nowait, msg)


hub = Hub()


def recursos() -> dict:
    dados = monitor.ler()
    dados["sessao"] = None
    if sessao:
        lat = getattr(sessao.transcritor, "ultima_latencia", None)
        dados["sessao"] = {
            "latencia_transcricao": lat,
            "latencia_llm": sessao.latencia_llm,
            "custo": sessao.custo,
            "ativa": sessao.ativa(),
        }
    dados["custo_mes"] = db.custo_mes()
    return dados


async def _monitorar():
    while True:
        try:
            hub.emitir({"tipo": "recursos", **await asyncio.to_thread(recursos)})
        except Exception as e:
            print(f"[monitor: {e}]")
        await asyncio.sleep(2)


@asynccontextmanager
async def ciclo(_app):
    # Os modelos só carregam ao iniciar uma reunião e são liberados ao encerrar,
    # pra não ocupar a memória da placa de vídeo com o app parado.
    hub.loop = asyncio.get_running_loop()
    tarefa = asyncio.create_task(_monitorar())
    yield
    tarefa.cancel()
    desligar()


app = FastAPI(lifespan=ciclo)


def desligar():
    """App fechando: encerra a reunião sem esperar a ata."""
    if sessao and sessao.ativa():
        sessao.encerrar()
        if sessao.id:
            db.atualizar_reuniao(sessao.id, status="encerrada", fim=time.time(), duracao=sessao.decorrido)


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    fila: asyncio.Queue = asyncio.Queue()
    hub.clientes.add(fila)
    try:
        estado = sessao.resumo() if sessao else {"estado": "parada"}
        await websocket.send_text(json.dumps({"tipo": "estado", **estado}, ensure_ascii=False))
        while True:
            await websocket.send_text(await fila.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.clientes.discard(fila)


# --- reunião ao vivo ---------------------------------------------------------


class Inicio(BaseModel):
    titulo: str = ""
    assunto: str = ""  # opcional, só desta reunião: ajuda o modelo sem fixar o tema
    intervalo: int | None = None


class Texto(BaseModel):
    texto: str


class Intervalo(BaseModel):
    segundos: int


def _sessao_ativa() -> Sessao:
    if not sessao or not sessao.ativa():
        raise HTTPException(409, "Nenhuma reunião em andamento")
    return sessao


@app.get("/api/estado")
def estado():
    return sessao.resumo() if sessao else {"estado": "parada"}


@app.post("/api/reuniao/iniciar")
def iniciar(corpo: Inicio):
    global sessao
    if sessao and sessao.ativa():
        raise HTTPException(409, "Já tem uma reunião em andamento")
    cfg = config()
    cfg["assunto"] = corpo.assunto.strip()
    if corpo.intervalo is not None:
        cfg["intervalo"] = corpo.intervalo
    sessao = Sessao(db, cfg, hub.emitir)
    sessao.iniciar(corpo.titulo)
    return sessao.resumo()


@app.post("/api/reuniao/pausar")
def pausar():
    _sessao_ativa().pausar()
    return sessao.resumo()


@app.post("/api/reuniao/retomar")
def retomar():
    _sessao_ativa().retomar()
    return sessao.resumo()


@app.post("/api/reuniao/encerrar")
def encerrar():
    _sessao_ativa().encerrar()
    return sessao.resumo()


@app.post("/api/reuniao/explicar")
def explicar():
    _sessao_ativa().explicar_agora()
    return {"ok": True}


@app.post("/api/reuniao/perguntar")
def perguntar(corpo: Texto):
    if not corpo.texto.strip():
        raise HTTPException(400, "Pergunta vazia")
    _sessao_ativa().perguntar(corpo.texto.strip())
    return {"ok": True}


@app.post("/api/reuniao/intervalo")
def intervalo(corpo: Intervalo):
    _sessao_ativa().mudar_intervalo(corpo.segundos)
    return sessao.resumo()


@app.post("/api/reuniao/titulo")
def titulo_atual(corpo: Texto):
    _sessao_ativa().renomear(corpo.texto.strip() or sessao.titulo)
    return sessao.resumo()


# --- histórico ---------------------------------------------------------------


@app.get("/api/reunioes")
def reunioes(busca: str = ""):
    return db.listar_reunioes(busca.strip())


@app.get("/api/reunioes/{rid}")
def reuniao(rid: int):
    r = db.obter_reuniao(rid)
    if not r:
        raise HTTPException(404, "Reunião não encontrada")
    return r


@app.patch("/api/reunioes/{rid}")
def renomear(rid: int, corpo: Texto):
    if sessao and sessao.id == rid and sessao.ativa():
        sessao.renomear(corpo.texto.strip())
    else:
        db.atualizar_reuniao(rid, titulo=corpo.texto.strip())
    return {"ok": True}


@app.delete("/api/reunioes/{rid}")
def apagar(rid: int):
    if sessao and sessao.id == rid and sessao.ativa():
        raise HTTPException(409, "Encerre a reunião antes de apagar")
    db.apagar_reuniao(rid)
    return {"ok": True}


def _markdown(r: dict) -> str:
    inicio = datetime.fromtimestamp(r["inicio"])
    partes = [
        f"# {r['titulo']}",
        f"{inicio:%d/%m/%Y %H:%M} · {mmss(r['duracao'] or 0)}",
    ]
    if r["ata"]:
        partes += ["", r["ata"]]
    if r["explicacoes"]:
        partes += ["", "## Copiloto"]
        for e in r["explicacoes"]:
            titulo = f"Pergunta: {e['pergunta']}" if e["tipo"] == "pergunta" else "Explicação"
            partes += ["", f"**[{mmss(e['t'])}] {titulo}**", "", e["texto"]]
    partes += ["", "## Transcrição", ""]
    partes += [f"- [{mmss(f['t'])}] **{f['quem']}:** {f['texto']}" for f in r["falas"]]
    return "\n".join(partes) + "\n"


@app.post("/api/reunioes/{rid}/ata")
def regerar_ata(rid: int):
    """Refaz a ata de uma reunião salva com os Ajustes atuais (modelo,
    vocabulário, contexto). Leva de alguns segundos a ~1 min."""
    if sessao and sessao.ativa():
        raise HTTPException(409, "Encerre a reunião em andamento antes de regerar uma ata")
    r = db.obter_reuniao(rid)
    if not r:
        raise HTTPException(404, "Reunião não encontrada")
    if not r["falas"]:
        raise HTTPException(400, "Essa reunião não tem falas")
    cfg = {**config(), "assunto": r.get("assunto") or ""}
    try:
        cerebro = criar_cerebro(cfg)
    except Exception as e:
        raise HTTPException(503, f"Não consegui carregar o modelo: {e}")
    try:
        _sugerido, texto, custo = gerar_ata(cerebro, r, lambda _t: None)
    except Exception as e:
        raise HTTPException(502, f"Não consegui gerar a ata: {e}")
    finally:
        if hasattr(cerebro, "liberar"):
            cerebro.liberar()
    db.atualizar_reuniao(rid, ata=texto)
    db.somar_custo(rid, custo)
    return {"ata": texto}


# --- revisão pós-reunião ---------------------------------------------------


class Troca(BaseModel):
    de: str
    para: str


class Aplicar(BaseModel):
    substituicoes: list[Troca] = []
    nova_ata: str = ""


class Aprender(BaseModel):
    termos: list[str] = []
    correcoes: list[Troca] = []
    contexto: list[str] = []


def _cerebro_revisao(cfg: dict):
    """(cérebro, limite de caracteres da transcrição). Sem aquecer: a revisão
    usa um contexto maior e o Ollama carregaria o modelo duas vezes."""
    if cfg["cerebro"] == "local":
        from cerebro_local import MAX_CHARS_REVISAO, CerebroLocal

        return CerebroLocal(cfg["modelo_local"], cfg.get("local_dispositivo", "gpu")), MAX_CHARS_REVISAO
    from cerebro import Cerebro

    # Reescrever a ata pede mais cuidado que as explicações ao vivo
    esforco = "medium" if cfg["esforco"] == "low" else cfg["esforco"]
    return Cerebro(cfg["modelo_claude"], esforco, cfg.get("anthropic_key") or None), None


@app.get("/api/reunioes/{rid}/revisao")
def ler_revisao(rid: int):
    return db.listar_revisao(rid)


@app.post("/api/reunioes/{rid}/revisao")
def revisar(rid: int, corpo: Texto):
    texto = corpo.texto.strip()
    if not texto:
        raise HTTPException(400, "Mensagem vazia")
    cfg = config()
    if cfg["cerebro"] == "local" and sessao and sessao.ativa():
        raise HTTPException(409, "Com o modelo local, revise depois de encerrar a reunião em andamento")
    r = db.obter_reuniao(rid)
    if not r:
        raise HTTPException(404, "Reunião não encontrada")
    cerebro, limite = _cerebro_revisao(cfg)
    anteriores = db.listar_revisao(rid)
    modo = revisao.intencao(texto)
    vazio = {"termos": [], "correcoes": [], "contexto": []}
    try:
        if modo == "pergunta":
            # Busca os trechos da transcrição com as palavras da pergunta
            sistema, mensagens = revisao.pedido_pergunta(r, cfg, texto, limite)
            resposta, custo = cerebro.completar(sistema, mensagens)
            dados = {"resposta": resposta.strip(), "substituicoes": [], "nova_ata": "", "sugestoes_contexto": vazio}
        elif modo == "ata":
            sistema, mensagens = revisao.pedido_editar_ata(r, texto, limite)
            nova, custo = cerebro.completar(sistema, mensagens)
            dados = {
                "resposta": "Reescrevi a ata com o que você pediu. Confira abaixo antes de aplicar.",
                "substituicoes": [], "nova_ata": revisao.ata_editada(nova), "sugestoes_contexto": vazio,
            }
        else:
            sistema = revisao.material(r, cfg, limite)
            mensagens = revisao.historico(anteriores, texto)
            bruto, custo = cerebro.revisar(sistema, mensagens, revisao.ESQUEMA)
            dados = revisao.normalizar(bruto, cfg, revisao.ja_sugerido(anteriores))
    except json.JSONDecodeError:
        raise HTTPException(502, "A IA respondeu fora do formato esperado; tente reformular")
    except Exception as e:
        raise HTTPException(502, f"Não consegui falar com o modelo: {e}")
    dados["modo"] = modo
    db.adicionar_revisao(rid, "usuario", texto)
    db.adicionar_revisao(rid, "ia", dados["resposta"], dados)
    db.somar_custo(rid, custo)
    return {**dados, "custo": custo}


@app.post("/api/reunioes/{rid}/aplicar")
def aplicar_revisao(rid: int, corpo: Aplicar):
    """Aplica as correções propostas na revisão: trocas na transcrição e nas
    explicações, e a ata reescrita (ou, sem ela, as mesmas trocas na ata)."""
    r = db.obter_reuniao(rid)
    if not r:
        raise HTTPException(404, "Reunião não encontrada")
    if sessao and sessao.id == rid and sessao.ativa():
        raise HTTPException(409, "Encerre a reunião antes de corrigi-la")
    trocar = revisao.trocador([t.model_dump() for t in corpo.substituicoes])
    textos = db.substituir_textos(rid, trocar)
    ata = corpo.nova_ata.strip() or (trocar(r["ata"]) if r["ata"] else None)
    if ata is not None and ata != r["ata"]:
        db.atualizar_reuniao(rid, ata=ata)
    return {"textos": textos, "ata": ata is not None and ata != r["ata"]}


@app.post("/api/config/aprender")
def aprender(corpo: Aprender):
    """Leva sugestões da revisão pros Ajustes (vocabulário, correções, contexto)."""
    cfg = config()
    novos = {
        "vocabulario": revisao.acrescentar_linhas(cfg["vocabulario"], corpo.termos),
        "correcoes": revisao.acrescentar_linhas(
            cfg["correcoes"],
            [f"{t.de} => {t.para}" for t in corpo.correcoes],
            chave=lambda l: l.split("=>")[0].strip().lower(),
        ),
        "contexto": revisao.acrescentar_linhas(cfg["contexto"], corpo.contexto),
    }
    db.salvar_config(novos)
    return ler_config()


@app.post("/api/reunioes/{rid}/exportar")
def exportar(rid: int):
    r = db.obter_reuniao(rid)
    if not r:
        raise HTTPException(404, "Reunião não encontrada")
    pasta = PASTA_DADOS / "exportacoes"
    pasta.mkdir(exist_ok=True)
    seguro = "".join(c if c.isalnum() or c in " -_" else "_" for c in r["titulo"]).strip()[:60]
    arq = pasta / f"{datetime.fromtimestamp(r['inicio']):%Y-%m-%d_%H%M} {seguro}.md"
    arq.write_text(_markdown(r), encoding="utf-8")
    return {"arquivo": str(arq)}


@app.post("/api/abrir-exportacoes")
def abrir_exportacoes():
    pasta = PASTA_DADOS / "exportacoes"
    pasta.mkdir(exist_ok=True)
    if sys.platform == "win32":
        os.startfile(pasta)
    else:
        subprocess.Popen(["xdg-open", str(pasta)])
    return {"ok": True}


# --- ajustes e recursos ------------------------------------------------------


@app.get("/api/config")
def ler_config():
    cfg = config()
    for chave in CHAVES_SECRETAS:
        cfg[chave + "_definida"] = bool(cfg[chave])
        cfg[chave] = ""
    return cfg


@app.put("/api/config")
def salvar_config(valores: dict):
    novos = {k: v for k, v in valores.items() if k in PADRAO}
    for chave in CHAVES_SECRETAS:  # vazio = manter a atual
        if not novos.get(chave):
            novos.pop(chave, None)
    db.salvar_config(novos)
    return ler_config()


@app.delete("/api/config/{chave}")
def apagar_chave(chave: str):
    if chave not in CHAVES_SECRETAS:
        raise HTTPException(400, "Só dá pra apagar chaves de API")
    db.salvar_config({chave: ""})
    return ler_config()


@app.get("/api/dispositivos")
def dispositivos():
    try:
        return listar_dispositivos()
    except RuntimeError as e:  # sem servidor de som (Linux)
        raise HTTPException(503, str(e))


@app.get("/api/ollama/modelos")
def ollama_modelos():
    return listar_modelos()


@app.get("/api/recursos")
def ler_recursos():
    return recursos()


app.mount("/", StaticFiles(directory=RAIZ / "ui", html=True), name="ui")
