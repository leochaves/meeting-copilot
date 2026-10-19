"""Uma reunião em andamento: captura → transcrição → explicações → ata.

Tudo roda em threads; a interface acompanha pelos eventos enviados a `emitir`.
"""

import queue
import threading
import time
from datetime import datetime

import numpy as np

import vocabulario
from audio import Capturador
from db import Db
from transcricao import Fala

NIVEL_A_CADA = 0.2  # s entre eventos de volume
MIN_PALAVRAS_ATA = 40  # abaixo disso o modelo inventa uma ata do nada


def mmss(seg: float) -> str:
    return f"{int(seg // 60):02d}:{int(seg % 60):02d}"


def criar_cerebro(cfg: dict):
    contexto = vocabulario.bloco_sistema(cfg.get("contexto", ""), vocabulario.termos(cfg.get("vocabulario", "")))
    if cfg["cerebro"] == "local":
        from cerebro_local import CerebroLocal

        c = CerebroLocal(cfg["modelo_local"], cfg.get("local_dispositivo", "gpu"), contexto)
        c.aquecer()
        return c
    from cerebro import Cerebro

    return Cerebro(cfg["modelo_claude"], cfg["esforco"], cfg.get("anthropic_key") or None, contexto)


def criar_transcritor(cfg: dict, ao_receber):
    termos = vocabulario.termos(cfg.get("vocabulario", ""))
    if cfg["transcricao"] == "deepgram":
        from transcricao import Transcritor

        return Transcritor(ao_receber, cfg.get("deepgram_key") or None, termos)
    from transcricao_whisper import TranscritorWhisper

    return TranscritorWhisper(
        ao_receber, cfg["whisper_modelo"], float(cfg["limiar"]),
        dispositivo=cfg.get("whisper_dispositivo", "gpu"),
        prompt=vocabulario.prompt_whisper(termos),
    )


class Sessao:
    def __init__(self, db: Db, cfg: dict, emitir):
        self.db = db
        self.cfg = cfg
        self._emitir = emitir

        self.id: int | None = None
        self.titulo = ""
        self.estado = "preparando"  # preparando | gravando | pausada | gerando_ata | encerrada | erro
        self.detalhe = ""
        self.intervalo = int(cfg["intervalo"])
        self.custo = 0.0
        self.latencia_llm: float | None = None
        self.transcritor = None
        self.cerebro = None

        self._parar = threading.Event()
        self._pausada = threading.Event()
        self._tarefas: queue.Queue = queue.Queue()
        self._pendentes: list[Fala] = []
        self._pend_lock = threading.Lock()
        self._t0 = 0.0  # time.monotonic() no início da gravação
        # Pausas em segundos de relógio desde _t0: [início, fim] (fim None = em curso)
        self._pausas: list[list[float | None]] = []
        self._correcoes = vocabulario.correcoes(cfg.get("correcoes", ""))
        self._proximo = 0.0
        self._ultimo_nivel = 0.0
        self._cap: Capturador | None = None

    # --- estado ------------------------------------------------------------

    def _relogio_real(self) -> float:
        return time.monotonic() - self._t0 if self._t0 else 0.0

    def _pausado_ate(self, t: float) -> float:
        """Quanto tempo ficou pausado entre o início e o instante `t` do relógio real."""
        total = 0.0
        for inicio, fim in self._pausas:
            if inicio < t:
                total += min(fim if fim is not None else t, t) - inicio
        return total

    @property
    def decorrido(self) -> float:
        """Tempo gravado: o relógio para durante as pausas."""
        t = self._relogio_real()
        return t - self._pausado_ate(t)

    def _tempo_gravado(self, t_audio: float) -> float:
        # O áudio segue (em silêncio) durante a pausa, então o tempo do
        # transcritor acompanha o relógio real; desconta as pausas anteriores.
        return t_audio - self._pausado_ate(t_audio)

    def resumo(self) -> dict:
        return {
            "id": self.id,
            "titulo": self.titulo,
            "estado": self.estado,
            "detalhe": self.detalhe,
            "decorrido": self.decorrido,
            "intervalo": self.intervalo,
            "custo": self.custo,
            "cerebro": getattr(self.cerebro, "modelo", None),
            "transcricao": getattr(self.transcritor, "descricao", None),
        }

    def _mudar(self, estado: str, detalhe: str = ""):
        self.estado, self.detalhe = estado, detalhe
        if self.id and estado in ("gravando", "pausada", "gerando_ata"):
            self.db.atualizar_reuniao(self.id, status=estado, duracao=self.decorrido)
        self._emitir({"tipo": "estado", **self.resumo()})

    def ativa(self) -> bool:
        return self.estado in ("preparando", "gravando", "pausada", "gerando_ata")

    # --- comandos (chamados pela API) -----------------------------------------

    def iniciar(self, titulo: str):
        self.titulo = titulo.strip() or f"Reunião de {datetime.now():%d/%m %H:%M}"
        threading.Thread(target=self._rodar, daemon=True).start()

    def pausar(self):
        if self.estado == "gravando":
            self._pausas.append([self._relogio_real(), None])
            self._pausada.set()
            self._mudar("pausada")

    def _fechar_pausa(self):
        if self._pausas and self._pausas[-1][1] is None:
            self._pausas[-1][1] = self._relogio_real()

    def retomar(self):
        if self.estado == "pausada":
            self._fechar_pausa()
            self._pausada.clear()
            self._proximo = time.monotonic() + self.intervalo
            self._mudar("gravando")

    def encerrar(self):
        self._fechar_pausa()
        self._pausada.clear()
        self._parar.set()

    def explicar_agora(self):
        self._proximo = time.monotonic() + self.intervalo
        self._tarefas.put(("explicacao", None))

    def perguntar(self, texto: str):
        self._tarefas.put(("pergunta", texto))

    def mudar_intervalo(self, segundos: int):
        self.intervalo = max(0, int(segundos))
        self._proximo = time.monotonic() + self.intervalo
        self._emitir({"tipo": "estado", **self.resumo()})

    def renomear(self, titulo: str):
        self.titulo = titulo
        if self.id:
            self.db.atualizar_reuniao(self.id, titulo=titulo)
        self._emitir({"tipo": "estado", **self.resumo()})

    # --- ciclo de vida -------------------------------------------------------

    def _rodar(self):
        trabalhador = None
        try:
            self._mudar("preparando", "Carregando o modelo de linguagem…")
            self.cerebro = criar_cerebro(self.cfg)
            self._mudar("preparando", "Carregando a transcrição…")
            self.transcritor = criar_transcritor(self.cfg, self._ao_receber)
            self._mudar("preparando", "Abrindo o áudio…")
            self._cap = Capturador(
                usar_mic=self.cfg["usar_mic"],
                mic_index=self.cfg.get("mic_index"),
                saida_index=self.cfg.get("saida_index"),
            )
            if self._parar.is_set():  # cancelou durante a preparação
                self._liberar()
                self._mudar("encerrada")
                return

            self.id = self.db.criar_reuniao(self.titulo, self.resumo()["cerebro"] or "")
            self._cap.iniciar()
            self._t0 = time.monotonic()
            self._proximo = self._t0 + self.intervalo
            dispositivos = f"🎙️ {self._cap.dev_mic['name'] if self._cap.dev_mic else 'sem microfone'} · 🔊 {self._cap.dev_sistema['name']}"
            self._mudar("gravando", dispositivos)

            trabalhador = threading.Thread(target=self._trabalhador, daemon=True)
            trabalhador.start()
            threading.Thread(target=self._relogio, daemon=True).start()

            self.transcritor.rodar(self._blocos(), self._parar)  # bloqueia até encerrar
        except Exception as e:
            self._emitir({"tipo": "erro", "mensagem": str(e)})
            if not self.id:
                self._liberar()
                self._mudar("erro", str(e))
                return
        finally:
            self._parar.set()
            if self._cap:
                self._cap.parar()

        self._finalizar(trabalhador)

    def _finalizar(self, trabalhador):
        # Espera a explicação em andamento terminar antes da ata
        if trabalhador:
            self._tarefas.put(None)
            trabalhador.join(timeout=180)
        duracao = self.decorrido
        self.db.atualizar_reuniao(self.id, duracao=duracao, fim=time.time())

        reuniao = self.db.obter_reuniao(self.id)
        palavras = sum(len(f["texto"].split()) for f in reuniao["falas"])
        if palavras >= MIN_PALAVRAS_ATA:
            self._mudar("gerando_ata", "Escrevendo a ata…")
            self._gerar_ata(reuniao)
        self.db.atualizar_reuniao(self.id, status="encerrada")
        self._liberar()
        self._mudar("encerrada")

    def _liberar(self):
        """Sem reunião, nada fica ocupando a memória da placa de vídeo."""
        for parte in (self.cerebro, self.transcritor):
            if hasattr(parte, "liberar"):
                try:
                    parte.liberar()
                except Exception as e:
                    print(f"[não consegui liberar {type(parte).__name__}: {e}]")

    def _gerar_ata(self, reuniao: dict):
        transcricao = "\n".join(f"[{mmss(f['t'])}] {f['quem']}: {f['texto']}" for f in reuniao["falas"])
        explicacoes = "\n\n".join(e["texto"] for e in reuniao["explicacoes"] if e["tipo"] == "explicacao")
        self._emitir({"tipo": "ata_inicio"})
        try:
            texto, custo = self.cerebro.gerar_ata(
                transcricao, explicacoes, lambda t: self._emitir({"tipo": "ata_delta", "texto": t})
            )
        except Exception as e:
            self._emitir({"tipo": "erro", "mensagem": f"Não consegui gerar a ata: {e}"})
            return
        self._somar_custo(custo)

        # 1ª linha "Título: ..." vira o título se o usuário não deu um
        linhas = texto.strip().splitlines()
        if linhas and linhas[0].lower().startswith("título:"):
            sugerido = linhas[0].split(":", 1)[1].strip().strip("`*\"")
            texto = "\n".join(linhas[1:]).strip()
            if sugerido and self.titulo.startswith("Reunião de "):
                self.renomear(sugerido)
        self.db.atualizar_reuniao(self.id, ata=texto)
        self._emitir({"tipo": "ata_fim", "texto": texto, "id": self.id})

    # --- áudio ---------------------------------------------------------------

    def _blocos(self):
        """Repassa o áudio, mede o volume e manda silêncio quando pausada."""
        silencio = None
        for bloco in self._cap.blocos(self._parar):
            agora = time.monotonic()
            if agora - self._ultimo_nivel >= NIVEL_A_CADA:
                self._ultimo_nivel = agora
                x = np.frombuffer(bloco, dtype=np.int16).astype(np.float32).reshape(-1, 2)
                rms = np.sqrt(np.mean(x**2, axis=0))
                self._emitir({
                    "tipo": "nivel",
                    "mic": float(min(1.0, rms[0] / 3000)),
                    "sistema": float(min(1.0, rms[1] / 3000)),
                    "decorrido": self.decorrido,
                })
            if self._pausada.is_set():
                silencio = silencio or bytes(len(bloco))
                yield silencio
            else:
                yield bloco

    def _ao_receber(self, fala: Fala):
        # Decide pelo momento em que foi falado: uma frase dita antes da pausa
        # mas transcrita depois dela continua valendo.
        if any(ini <= fala.t < (fim if fim is not None else float("inf")) for ini, fim in self._pausas):
            return
        texto = vocabulario.corrigir(fala.texto, self._correcoes)
        fala = Fala(t=self._tempo_gravado(fala.t), quem=fala.quem, texto=texto)
        with self._pend_lock:
            self._pendentes.append(fala)
        if self.id:
            self.db.adicionar_fala(self.id, fala.t, fala.quem, fala.texto)
        self._emitir({"tipo": "fala", "t": fala.t, "quem": fala.quem, "texto": fala.texto})

    # --- explicações ---------------------------------------------------------

    def _relogio(self):
        while not self._parar.is_set():
            time.sleep(1)
            if self._pausada.is_set() or self.intervalo <= 0:
                continue
            if time.monotonic() >= self._proximo:
                self._proximo = time.monotonic() + self.intervalo
                self._tarefas.put(("explicacao", "auto"))

    def _tirar_pendentes(self) -> str:
        with self._pend_lock:
            falas, self._pendentes = self._pendentes, []
        if not falas:
            return ""
        linhas, ultimo = [], None
        for f in falas:  # junta falas seguidas da mesma pessoa
            if linhas and f.quem == ultimo:
                linhas[-1] += " " + f.texto
            else:
                linhas.append(f"{f.quem}: {f.texto}")
                ultimo = f.quem
        return f"Trecho [{mmss(falas[0].t)}–{mmss(falas[-1].t)}]:\n" + "\n".join(linhas)

    def _trabalhador(self):
        while True:
            tarefa = self._tarefas.get()
            if tarefa is None:
                return
            tipo, extra = tarefa
            trecho = self._tirar_pendentes()
            if tipo == "explicacao":
                if not trecho:
                    if extra != "auto":
                        self._emitir({"tipo": "aviso", "mensagem": "Nada novo desde a última explicação."})
                    continue
                self._consultar(trecho + "\n\nExplique esse trecho.", "explicacao", None)
            else:
                conteudo = (trecho + "\n\n" if trecho else "") + f"Minha pergunta: {extra}"
                self._consultar(conteudo, "pergunta", extra)

    def _consultar(self, conteudo: str, tipo: str, pergunta: str | None):
        t = self.decorrido
        self._emitir({"tipo": "explicacao_inicio", "modo": tipo, "pergunta": pergunta, "t": t})
        inicio = time.monotonic()
        try:
            texto, custo = self.cerebro.enviar(
                conteudo, lambda d: self._emitir({"tipo": "explicacao_delta", "texto": d})
            )
        except Exception as e:
            self._emitir({"tipo": "explicacao_fim", "erro": str(e)})
            return
        segundos = time.monotonic() - inicio
        self.latencia_llm = segundos
        self._somar_custo(custo)
        self.db.adicionar_explicacao(self.id, t, tipo, pergunta, texto, segundos, custo)
        self._emitir({"tipo": "explicacao_fim", "texto": texto, "segundos": segundos, "custo": custo})

    def _somar_custo(self, valor: float):
        if valor:
            self.custo += valor
            self.db.somar_custo(self.id, valor)
