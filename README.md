# Copiloto de Reuniões

Ouve a reunião (Meet, Zoom, Teams, qualquer uma), transcreve ao vivo e, de
tempos em tempos ou quando você pedir, explica o que está rolando e sugere o
que dizer. No fim, escreve a ata: resumo, decisões, tarefas e pendências.

Roda no seu PC. O áudio nunca sai da máquina. No modo local, nada sai.

## Antes de instalar

| | Obrigatório | Observação |
|---|---|---|
| **Windows 10 ou 11, 64 bits** | ✅ | A captura do áudio do PC usa o WASAPI do Windows |
| **~10 GB livres no disco** | ✅ | Python + bibliotecas (~3 GB), Whisper (~1,6 GB), Ollama + modelo (~5 GB) |
| **Internet durante a instalação** | ✅ | Depois, o modo local funciona offline |
| **winget** (Instalador de Aplicativo) | ✅ | Já vem no Windows 11 e no 10 atualizado. Se faltar: Microsoft Store → "Instalador de Aplicativo" |
| **Placa de vídeo NVIDIA** (6 GB+) | Recomendado | Com ela, transcrição e explicações levam segundos. Sem ela, tudo roda no processador, bem mais devagar (veja abaixo) |
| **Driver NVIDIA atualizado** | Se tiver NVIDIA | Baixe pelo app NVIDIA ou em nvidia.com/drivers |
| **Fone de ouvido** | Recomendado | Com caixa de som, o microfone também capta os outros e duplica as falas |
| **Chave da API do Claude** | Opcional | Só pro modo Claude. Crie em platform.claude.com → API Keys (pago por uso, centavos por reunião) |

Python, Ollama, WebView2 e os modelos são instalados pelo instalador.

## Instalar

1. Baixe o projeto: **Code → Download ZIP** e extraia, ou rode
   `git clone https://github.com/leochaves/meeting-copilot`.
2. Clique duas vezes em **`Instalar.bat`**.
3. Responda se quer o **modo local** (Ollama, ~5 GB). Sem ele, o app usa só
   o Claude.

O instalador:

- instala o Python 3.12, se não houver um de 3.10 a 3.13;
- cria o ambiente `.venv` e instala as bibliotecas (e as da NVIDIA, se
  houver placa);
- instala o WebView2, se faltar (o motor da janela do app);
- baixa o modelo do Whisper (`large-v3-turbo` com NVIDIA, `small` sem);
- instala o Ollama e baixa o `gemma3:4b`, se você quiser o modo local;
- cria o atalho **Copiloto de Reuniões** na Área de Trabalho.

Pode rodar de novo quando quiser: o que já estiver instalado é pulado. O
Windows pode pedir confirmação (UAC) ao instalar Python, WebView2 ou Ollama.

Opções pela linha de comando: `Instalar.bat -ComOllama`, `-SemOllama`,
`-SemAtalho`, `-ModeloLocal qwen3:4b`.

### Sem placa NVIDIA

O instalador detecta e já deixa configurado: Whisper `small` na CPU,
explicações a cada 2 min. Medido num Ryzen 7 2700:

| | GPU (RTX 4060) | CPU |
|---|---|---|
| Transcrever uma frase de 4,5 s | ~1 s | ~6 s (`small`) |
| Uma explicação (gemma3:4b) | ~4 s | ~45 s |

Sem NVIDIA, a melhor combinação é **Whisper na CPU + Claude** pras
explicações.

## Usar

Abra pelo atalho, ou por `Copiloto.bat`.

- **Ao vivo**: ▶ Iniciar · ⏸ Pausar · ⏹ Encerrar; transcrição; cartões de
  explicação; perguntas; medidores de volume do mic e do PC.
- **Reuniões**: histórico com busca; ata, explicações e transcrição; exportar
  `.md`; renomear; apagar.
- **Recursos**: memória e uso da placa de vídeo, CPU, RAM, tempos de resposta,
  custo do Claude.
- **Ajustes**: Claude ou local; Whisper ou Deepgram; GPU ou CPU; microfone e
  saída; sensibilidade; intervalo; chaves de API.

Iniciar leva ~15 s: os modelos carregam na placa só durante a reunião e são
liberados ao encerrar.

Avise os participantes de que a reunião está sendo transcrita (LGPD).

## Como funciona

```
┌──────────── janela (ui/, pywebview) ────────┐
│  Ao vivo · Reuniões · Recursos · Ajustes    │
└──────────────┬──────────────────────────────┘
               │ HTTP + WebSocket (127.0.0.1:8765)
┌──────────────▼──────────────────────────────┐
│ servidor.py (FastAPI) → sessao.py            │
│ audio.py: mic + loopback do Windows          │
│ → transcricao_whisper.py / transcricao.py    │
│ → cerebro.py (Claude) / cerebro_local.py     │
│ db.py: dados/copiloto.db (SQLite, só local)  │
└──────────────────────────────────────────────┘
```

| Arquivo | O quê |
|---|---|
| `app.py` | Abre o servidor local e a janela |
| `servidor.py` | API e eventos ao vivo pra interface |
| `sessao.py` | Uma reunião: iniciar/pausar/encerrar, explicações, ata |
| `audio.py` | Captura do microfone + áudio do PC (loopback WASAPI) |
| `transcricao_whisper.py` | Whisper local (GPU ou CPU) |
| `transcricao.py` | Deepgram (nuvem, separa quem fala) |
| `cerebro.py` / `cerebro_local.py` | Explicações e ata: Claude / Ollama |
| `monitor.py` | GPU, CPU, RAM |
| `db.py` | Banco local |
| `ui/` | Interface (HTML/CSS/JS) |

## Problemas comuns

- **Nada é transcrito**: veja os medidores "Você" e "Outros" na tela Ao vivo.
  Se estiverem parados, escolha o microfone e a saída certos em Ajustes.
- **Tudo muito lento**: na tela Recursos, se a memória de vídeo passar de ~92%,
  feche apps pesados (jogos, Docker) ou use um modelo menor.
- **Ruído virando texto**: aumente a sensibilidade em Ajustes (padrão 250).
- **Erros**: veja `dados/copiloto.log`.

## Desinstalar

Apague a pasta do projeto e o atalho da Área de Trabalho. Os modelos ficam em
`%USERPROFILE%\.cache\huggingface` (Whisper) e no Ollama (`ollama rm
gemma3:4b`, ou desinstale o Ollama pelo Windows).
