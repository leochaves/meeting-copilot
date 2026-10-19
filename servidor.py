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
from audio import listar_dispositivos
from cerebro_local import listar_modelos
from db import PASTA_DADOS, Db
from sessao import Sessao, mmss

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
