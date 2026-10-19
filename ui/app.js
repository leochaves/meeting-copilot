"use strict";

const $ = (sel, raiz = document) => raiz.querySelector(sel);
const $$ = (sel, raiz = document) => [...raiz.querySelectorAll(sel)];

// --- utilitários -------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inline(s) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
}

/** Markdown mínimo: títulos, listas, negrito e parágrafos. */
function md(texto) {
  const html = [];
  let lista = false;
  for (const linha of String(texto || "").split("\n")) {
    const l = linha.trim();
    const item = l.match(/^[-*•]\s+(.*)/) || l.match(/^\d+[.)]\s+(.*)/);
    if (item) {
      if (!lista) { html.push("<ul>"); lista = true; }
      html.push(`<li>${inline(item[1])}</li>`);
      continue;
    }
    if (lista) { html.push("</ul>"); lista = false; }
    const titulo = l.match(/^(#{1,3})\s+(.*)/);
    if (titulo) html.push(`<h${titulo[1].length}>${inline(titulo[2])}</h${titulo[1].length}>`);
    else if (l) html.push(`<p>${inline(l)}</p>`);
  }
  if (lista) html.push("</ul>");
  return html.join("");
}

function mmss(seg) {
  seg = Math.max(0, Math.floor(seg || 0));
  const h = Math.floor(seg / 3600), m = Math.floor((seg % 3600) / 60), s = seg % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}

function gb(bytes) {
  return (bytes / 1024 ** 3).toLocaleString("pt-BR", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
}

function usd(v) {
  if (!v) return "US$ 0";
  return "US$ " + v.toLocaleString("pt-BR", { maximumFractionDigits: v < 0.1 ? 4 : 2 });
}

function data(ts) {
  return new Date(ts * 1000).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

function toast(msg, erro = false) {
  const el = document.createElement("div");
  el.className = "toast" + (erro ? " erro" : "");
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), erro ? 7000 : 3500);
}

async function copiar(texto) {
  try {
    await navigator.clipboard.writeText(texto);
  } catch {
    // Reserva pra janelas onde a API da área de transferência não existe
    const area = document.createElement("textarea");
    area.value = texto;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast("Copiado.");
}

function botaoCopiar(texto, rotulo = "📋 Copiar") {
  const b = document.createElement("button");
  b.className = "btn pequeno";
  b.type = "button";
  b.textContent = rotulo;
  b.addEventListener("click", () => copiar(texto));
  return b;
}

async function api(caminho, opcoes = {}) {
  const r = await fetch(caminho, {
    headers: { "Content-Type": "application/json" },
    ...opcoes,
    body: opcoes.body !== undefined ? JSON.stringify(opcoes.body) : undefined,
  });
  const corpo = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(corpo.detail || `Erro ${r.status}`);
  return corpo;
}

async function tentar(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

function preencherTrilho(el, fracao) {
  const f = Math.max(0, Math.min(1, fracao || 0));
  el.style.width = (f * 100).toFixed(1) + "%";
  el.classList.toggle("alto", f > 0.8 && f <= 0.92);
  el.classList.toggle("critico", f > 0.92);
}

// --- navegação ---------------------------------------------------------------

function irPara(tela) {
  $$(".nav > button").forEach((b) => b.classList.toggle("ativo", b.dataset.tela === tela));
  $$(".tela").forEach((t) => t.classList.toggle("ativa", t.id === "tela-" + tela));
  if (tela === "reunioes") { fecharDetalhe(); carregarLista(); }
  if (tela === "ajustes") carregarAjustes();
}
$$(".nav > button").forEach((b) => b.addEventListener("click", () => irPara(b.dataset.tela)));

// --- ao vivo -------------------------------------------------------------------

const ROTULOS = {
  parada: "Parado", preparando: "Preparando…", gravando: "Gravando", pausada: "Pausada",
  gerando_ata: "Escrevendo ata…", encerrada: "Encerrada", erro: "Erro",
};
let estado = { estado: "parada" };
let sessaoAtual = null;   // id da reunião exibida nos painéis
let cartaoAtual = null;   // cartão recebendo texto em streaming
let cartaoAta = null;

function ativa(e = estado.estado) {
  return ["preparando", "gravando", "pausada", "gerando_ata"].includes(e);
}

function aplicarEstado(novo) {
  const anterior = estado;
  estado = novo;
  const e = novo.estado;

  // Nova reunião começou: limpa os painéis
  if (e === "preparando" && !ativa(anterior.estado)) limparPaineis();
  if (novo.id && novo.id !== sessaoAtual) sessaoAtual = novo.id;

  const pill = $("#pill");
  pill.textContent = ROTULOS[e] || e;
  pill.className = "pill " + e;
  $("#detalhe").textContent = novo.detalhe || "";
  if (novo.decorrido !== undefined && ativa(e)) $("#relogio").textContent = mmss(novo.decorrido);

  $("#btn-iniciar").hidden = ativa(e);
  $("#btn-pausar").hidden = e !== "gravando";
  $("#btn-retomar").hidden = e !== "pausada";
  $("#btn-encerrar").hidden = !["preparando", "gravando", "pausada"].includes(e);
  $("#btn-encerrar").textContent = e === "preparando" ? "✕ Cancelar" : "⏹ Encerrar";

  const podeFalar = e === "gravando" || e === "pausada";
  $("#btn-explicar").disabled = !podeFalar;
  $("#pergunta").disabled = !podeFalar;
  $("#form-pergunta button").disabled = !podeFalar;

  const titulo = $("#titulo");
  if (ativa(e) && document.activeElement !== titulo) titulo.value = novo.titulo || "";
  if (e === "encerrada" && anterior.estado !== "encerrada") titulo.value = "";

  if (novo.intervalo !== undefined && ativa(e)) $("#intervalo").value = String(novo.intervalo);
  if (!ativa(e)) { preencherTrilho($("#nivel-mic"), 0); preencherTrilho($("#nivel-sis"), 0); }
  if (e === "erro" && novo.detalhe) toast(novo.detalhe, true);
}

function limparPaineis() {
  $("#falas").innerHTML = "";
  $("#cartoes").innerHTML = "";
  $("#relogio").textContent = "00:00";
  cartaoAtual = cartaoAta = null;
}

function rolarSeNoFim(el, fn) {
  const noFim = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  fn();
  if (noFim) el.scrollTop = el.scrollHeight;
}

function adicionarFala(f) {
  const lista = $("#falas");
  $(".vazio", lista)?.remove();
  rolarSeNoFim(lista, () => {
    const el = document.createElement("div");
    el.className = "fala";
    el.innerHTML = `<time>${mmss(f.t)}</time><span class="quem ${f.quem === "Você" ? "voce" : ""}">${esc(f.quem)}</span><span>${esc(f.texto)}</span>`;
    lista.append(el);
  });
}

function novoCartao(classe, titulo, t) {
  const lista = $("#cartoes");
  $(".vazio", lista)?.remove();
  $$(".cartao.novo", lista).forEach((c) => c.classList.remove("novo"));
  const el = document.createElement("article");
  el.className = "cartao novo " + classe;
  el.innerHTML = `<header><b>${esc(titulo)}</b>${t !== undefined ? `<time>${mmss(t)}</time>` : ""}</header>
    <div class="corpo"><span class="pensando">pensando</span></div><footer hidden></footer>`;
  lista.prepend(el);
  lista.scrollTop = 0;
  return el;
}

function escreverNoCartao(cartao, texto) {
  if (!cartao) return;
  const corpo = $(".corpo", cartao);
  if ($(".pensando", corpo)) corpo.textContent = "";
  corpo.append(texto);
}

function tratarEvento(ev) {
  switch (ev.tipo) {
    case "estado":
      aplicarEstado(ev);
      break;
    case "nivel":
      preencherTrilho($("#nivel-mic"), ev.mic);
      preencherTrilho($("#nivel-sis"), ev.sistema);
      $("#relogio").textContent = mmss(ev.decorrido);
      break;
    case "fala":
      adicionarFala(ev);
      break;
    case "explicacao_inicio":
      cartaoAtual = novoCartao(ev.modo, ev.modo === "pergunta" ? `❓ ${ev.pergunta}` : "Explicação", ev.t);
      break;
    case "explicacao_delta":
      escreverNoCartao(cartaoAtual, ev.texto);
      break;
    case "explicacao_fim":
      if (cartaoAtual) {
        const rodape = $("footer", cartaoAtual);
        if (ev.erro) {
          cartaoAtual.classList.add("erro");
          $(".corpo", cartaoAtual).textContent = ev.erro;
        } else {
          rodape.hidden = false;
          rodape.textContent = `${ev.segundos.toFixed(1)} s` + (ev.custo ? ` · ${usd(ev.custo)}` : "");
          rodape.append(botaoCopiar(ev.texto));
        }
      }
      cartaoAtual = null;
      break;
    case "ata_inicio":
      cartaoAta = novoCartao("ata", "📝 Ata da reunião");
      break;
    case "ata_delta":
      escreverNoCartao(cartaoAta, ev.texto);
      break;
    case "ata_fim":
      if (cartaoAta) {
        const corpo = $(".corpo", cartaoAta);
        corpo.classList.add("md");
        corpo.innerHTML = md(ev.texto);
        const rodape = $("footer", cartaoAta);
        rodape.hidden = false;
        rodape.innerHTML = `<button class="btn pequeno">Abrir reunião</button>`;
        $("button", rodape).addEventListener("click", () => { irPara("reunioes"); abrirReuniao(ev.id); });
        rodape.append(botaoCopiar(ev.texto, "📋 Copiar ata"));
      }
      cartaoAta = null;
      break;
    case "aviso":
      toast(ev.mensagem);
      break;
    case "erro":
      toast(ev.mensagem, true);
      break;
    case "recursos":
      mostrarRecursos(ev);
      break;
  }
}

$("#btn-iniciar").addEventListener("click", () =>
  tentar(async () => aplicarEstado(await api("/api/reuniao/iniciar", { method: "POST", body: { titulo: $("#titulo").value, intervalo: +$("#intervalo").value } }))));
$("#btn-pausar").addEventListener("click", () => tentar(() => api("/api/reuniao/pausar", { method: "POST" })));
$("#btn-retomar").addEventListener("click", () => tentar(() => api("/api/reuniao/retomar", { method: "POST" })));
$("#btn-encerrar").addEventListener("click", () => tentar(() => api("/api/reuniao/encerrar", { method: "POST" })));
$("#btn-explicar").addEventListener("click", () => tentar(() => api("/api/reuniao/explicar", { method: "POST" })));
$("#intervalo").addEventListener("change", (e) => {
  if (ativa()) tentar(() => api("/api/reuniao/intervalo", { method: "POST", body: { segundos: +e.target.value } }));
});
$("#titulo").addEventListener("change", (e) => {
  if (ativa()) tentar(() => api("/api/reuniao/titulo", { method: "POST", body: { texto: e.target.value } }));
});
$("#titulo").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.target.blur(); if (!ativa()) $("#btn-iniciar").click(); }
});
$("#form-pergunta").addEventListener("submit", (e) => {
  e.preventDefault();
  const campo = $("#pergunta");
  const texto = campo.value.trim();
  if (!texto) return;
  campo.value = "";
  tentar(() => api("/api/reuniao/perguntar", { method: "POST", body: { texto } }));
});

// --- WebSocket -----------------------------------------------------------------

function conectar() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  const indicador = $("#nav-conexao");
  ws.onopen = () => { indicador.textContent = "conectado"; indicador.classList.add("ok"); };
  ws.onmessage = (m) => tratarEvento(JSON.parse(m.data));
  ws.onclose = () => {
    indicador.textContent = "reconectando…";
    indicador.classList.remove("ok");
    setTimeout(conectar, 1500);
  };
}

// --- reuniões ------------------------------------------------------------------

const STATUS = {
  gravando: "gravando", pausada: "pausada", gerando_ata: "escrevendo ata",
  encerrada: "encerrada", interrompida: "interrompida",
};
let buscaTimer;

async function carregarLista() {
  const busca = $("#busca").value;
  const lista = await tentar(() => api("/api/reunioes?busca=" + encodeURIComponent(busca)));
  if (!lista) return;
  const el = $("#lista");
  if (!lista.length) {
    el.innerHTML = `<p class="vazio">${busca ? "Nada encontrado." : "Nenhuma reunião ainda. Comece uma na tela Ao vivo."}</p>`;
    return;
  }
  el.innerHTML = lista.map((r) => `
    <button class="item" data-id="${r.id}">
      <div><div class="nome">${esc(r.titulo)}</div>
        <div class="sub">${data(r.inicio)} · ${r.n_falas} falas${r.tem_ata ? " · ata" : ""}</div></div>
      <span class="sub">${mmss(r.duracao)}</span>
      <span class="sub">${r.custo ? usd(r.custo) : ""}</span>
      <span class="etiqueta ${r.status}">${STATUS[r.status] || r.status}</span>
    </button>`).join("");
  $$(".item", el).forEach((b) => b.addEventListener("click", () => abrirReuniao(+b.dataset.id)));
}

$("#busca").addEventListener("input", () => { clearTimeout(buscaTimer); buscaTimer = setTimeout(carregarLista, 250); });
$("#btn-pasta").addEventListener("click", () => tentar(() => api("/api/abrir-exportacoes", { method: "POST" })));

function fecharDetalhe() {
  $("#reuniao-detalhe").hidden = true;
  $("#reunioes-lista").hidden = false;
}

async function abrirReuniao(id) {
  const r = await tentar(() => api("/api/reunioes/" + id));
  if (!r) return;
  $("#reunioes-lista").hidden = true;
  const det = $("#reuniao-detalhe");
  det.hidden = false;
  det.innerHTML = `
    <header class="topo">
      <button class="btn" id="voltar">← Voltar</button>
      <input class="titulo-input" id="det-titulo" value="${esc(r.titulo)}" maxlength="120">
      <div class="acoes">
        <button class="btn" id="det-exportar">⬇ Exportar .md</button>
        <button class="btn perigo" id="det-apagar">Apagar</button>
      </div>
    </header>
    <div class="detalhe">${data(r.inicio)} · ${mmss(r.duracao)} · ${esc(r.cerebro || "")}${r.custo ? " · " + usd(r.custo) : ""} · ${STATUS[r.status] || r.status}</div>
    <nav class="abas">
      <button data-aba="ata">Ata</button>
      <button data-aba="copiloto">Copiloto (${r.explicacoes.length})</button>
      <button data-aba="transcricao">Transcrição (${r.falas.length})</button>
    </nav>
    <div class="conteudo-aba" id="det-conteudo"></div>`;

  const abas = {
    ata: () => r.ata ? `<div class="md">${md(r.ata)}</div>`
      : `<p class="vazio">${r.falas.length ? "Sem ata: a reunião foi curta demais, foi interrompida ou a geração falhou." : "Nenhuma fala registrada."}</p>`,
    copiloto: () => r.explicacoes.length ? r.explicacoes.map((e) => `
      <article class="cartao"><header><b>${e.tipo === "pergunta" ? "❓ " + esc(e.pergunta) : "Explicação"}</b><time>${mmss(e.t)}</time></header>
      <div class="corpo">${esc(e.texto)}</div></article>`).join("") : `<p class="vazio">Sem explicações.</p>`,
    transcricao: () => r.falas.length ? r.falas.map((f) => `
      <div class="fala"><time>${mmss(f.t)}</time><span class="quem ${f.quem === "Você" ? "voce" : ""}">${esc(f.quem)}</span><span>${esc(f.texto)}</span></div>`).join("")
      : `<p class="vazio">Nenhuma fala registrada.</p>`,
  };
  const textoTranscricao = () => r.falas.map((f) => `[${mmss(f.t)}] ${f.quem}: ${f.texto}`).join("\n");
  const mostrar = (aba) => {
    $$(".abas button", det).forEach((b) => b.classList.toggle("ativo", b.dataset.aba === aba));
    const conteudo = $("#det-conteudo");
    conteudo.innerHTML = abas[aba]();
    // Botões de copiar: a ata e a transcrição inteiras, e cada explicação
    const barra = document.createElement("div");
    barra.className = "barra-copiar";
    if (aba === "ata" && r.ata) barra.append(botaoCopiar(r.ata, "📋 Copiar ata"));
    if (aba === "transcricao" && r.falas.length) barra.append(botaoCopiar(textoTranscricao(), "📋 Copiar transcrição"));
    if (barra.children.length) conteudo.prepend(barra);
    if (aba === "copiloto") {
      $$(".cartao", conteudo).forEach((c, i) => {
        const rodape = document.createElement("footer");
        rodape.append(botaoCopiar(r.explicacoes[i].texto));
        c.append(rodape);
      });
    }
  };
  $$(".abas button", det).forEach((b) => b.addEventListener("click", () => mostrar(b.dataset.aba)));
  mostrar(r.ata ? "ata" : r.explicacoes.length ? "copiloto" : "transcricao");

  $("#voltar").addEventListener("click", () => { fecharDetalhe(); carregarLista(); });
  $("#det-titulo").addEventListener("change", (e) =>
    tentar(async () => { await api("/api/reunioes/" + id, { method: "PATCH", body: { texto: e.target.value } }); toast("Título salvo."); }));
  $("#det-exportar").addEventListener("click", () =>
    tentar(async () => { const { arquivo } = await api(`/api/reunioes/${id}/exportar`, { method: "POST" }); toast("Exportado: " + arquivo); }));
  const apagar = $("#det-apagar");
  apagar.addEventListener("click", () => {
    if (!apagar.dataset.confirmar) {
      apagar.dataset.confirmar = "1";
      apagar.textContent = "Clique de novo pra apagar";
      setTimeout(() => { delete apagar.dataset.confirmar; apagar.textContent = "Apagar"; }, 4000);
      return;
    }
    tentar(async () => {
      await api("/api/reunioes/" + id, { method: "DELETE" });
      toast("Reunião apagada.");
      fecharDetalhe();
      carregarLista();
    });
  });
}

// --- recursos ------------------------------------------------------------------

function seg(v) {
  return v == null ? "—" : v.toLocaleString("pt-BR", { maximumFractionDigits: 1 }) + " s";
}

function mostrarRecursos(d) {
  // Barrinhas da navegação: memória ocupada ≠ processamento
  const navVram = $("#nav-vram"), navUso = $("#nav-uso");
  if (d.gpu) {
    const f = d.gpu.vram_usada / d.gpu.vram_total;
    preencherTrilho($("i", navVram), f);
    $("b", navVram).textContent = Math.round(f * 100) + "%";
    preencherTrilho($("i", navUso), d.gpu.uso / 100);
    $("b", navUso).textContent = d.gpu.uso + "%";
  } else { navVram.style.display = "none"; navUso.style.display = "none"; }

  if (!$("#tela-recursos").classList.contains("ativa")) return;

  const cards = [];
  if (d.gpu) {
    const f = d.gpu.vram_usada / d.gpu.vram_total;
    cards.push(`<div class="card">
      <span class="rotulo">Memória da placa de vídeo (ocupada)</span>
      <span class="valor">${gb(d.gpu.vram_usada)} <small>de ${gb(d.gpu.vram_total)} GB</small></span>
      <div class="trilho"><i style="width:${(f * 100).toFixed(1)}%" class="${f > 0.92 ? "critico" : f > 0.8 ? "alto" : ""}"></i></div>
      <span class="nota ${f > 0.92 ? "alerta" : ""}">${f > 0.92
        ? "Quase cheia: Whisper e modelo local podem ficar muito lentos. Feche apps pesados."
        : "Espaço reservado por todos os programas. Ocupar memória não é processar — não gasta energia."}</span>
    </div>`);
    cards.push(`<div class="card">
      <span class="rotulo">Processamento da placa de vídeo (uso)</span>
      <span class="valor">${d.gpu.uso}<small>%</small></span>
      <div class="trilho"><i style="width:${d.gpu.uso}%" class="${d.gpu.uso > 92 ? "critico" : d.gpu.uso > 80 ? "alto" : ""}"></i></div>
      <span class="nota">${esc(d.gpu.nome)} · trabalho real de todos os programas (tela, navegador, jogos…). O Copiloto só processa durante a reunião.</span>
    </div>`);
  }
  const fr = d.ram_usada / d.ram_total;
  cards.push(`<div class="card">
    <span class="rotulo">Processador</span>
    <span class="valor">${Math.round(d.cpu)}<small>%</small></span>
    <div class="trilho"><i style="width:${d.cpu}%" class="${d.cpu > 92 ? "critico" : d.cpu > 80 ? "alto" : ""}"></i></div>
    <span class="nota">RAM ${gb(d.ram_usada)} de ${gb(d.ram_total)} GB (${Math.round(fr * 100)}%)</span>
  </div>`);

  const s = d.sessao;
  cards.push(`<div class="card">
    <span class="rotulo">Desempenho ${s?.ativa ? "· reunião em andamento" : ""}</span>
    <div class="linha"><span>Transcrição (fim da frase → texto)</span><span>${seg(s?.latencia_transcricao)}</span></div>
    <div class="linha"><span>Última explicação</span><span>${seg(s?.latencia_llm)}</span></div>
    <span class="nota">Se a transcrição passar de ~5 s ou a explicação de ~30 s, algo está apertado (veja a memória de vídeo).</span>
  </div>`);

  cards.push(`<div class="card">
    <span class="rotulo">Custo do Claude</span>
    <span class="valor">${usd(d.custo_mes)} <small>este mês</small></span>
    <div class="linha"><span>Reunião atual</span><span>${usd(s?.custo || 0)}</span></div>
    <span class="nota">Estimativa pelos tokens usados. Modelo local e Whisper são grátis.</span>
  </div>`);

  cards.push(`<div class="card">
    <span class="rotulo">Modelos carregados no Ollama</span>
    ${d.ollama.length ? d.ollama.map((m) => `<div class="linha"><span>${esc(m.nome)}</span><span>${gb(m.vram)} GB na GPU${m.vram < m.total ? " · parte na CPU" : ""}</span></div>`).join("")
      : `<span class="nota">Nenhum (ou o Ollama está fechado).</span>`}
  </div>`);

  $("#recursos").innerHTML = cards.join("");
}

// --- ajustes -------------------------------------------------------------------

const MODELOS_CLAUDE = [
  ["claude-opus-5-5", "Opus 5.5 — mais inteligente (~US$ 0,90/h)"],
  ["claude-sonnet-5-5", "Sonnet 5.5 — equilíbrio (~US$ 0,55/h)"],
  ["claude-haiku-5-5", "Haiku 5.5 — mais barato (~US$ 0,03/h)"],
];

const DISPOSITIVOS = [
  ["gpu", "Placa de vídeo (GPU) — recomendado"],
  ["cpu", "Processador (CPU) — sem placa NVIDIA, bem mais lento"],
];

function opcoes(lista, atual) {
  return lista.map(([v, rotulo]) => `<option value="${esc(v)}" ${String(v) === String(atual) ? "selected" : ""}>${esc(rotulo)}</option>`).join("");
}

async function carregarAjustes() {
  const [cfg, disp, locais] = await Promise.all([
    api("/api/config"), api("/api/dispositivos").catch(() => ({ mics: [], saidas: [] })), api("/api/ollama/modelos").catch(() => []),
  ]).catch((e) => { toast(e.message, true); return []; });
  if (!cfg) return;
  if (!locais.includes(cfg.modelo_local)) locais.unshift(cfg.modelo_local);

  $("#form-ajustes").innerHTML = `
    <fieldset class="largo">
      <legend>Vocabulário e contexto</legend>
      <span class="ajuda">Ajuda a transcrição e o modelo a acertar nomes de pessoas, empresas e tecnologias.</span>
      <div class="tres-colunas">
        <label>Nomes e termos
          <textarea name="vocabulario" rows="7" placeholder="Leonardo&#10;Bê&#10;PlannerFit&#10;Kubernetes&#10;Supabase">${esc(cfg.vocabulario)}</textarea>
          <span class="ajuda">Um por linha (ou separados por vírgula). Vão como dica pra transcrição e pro modelo.</span></label>
        <label>Correções
          <textarea name="correcoes" rows="7" placeholder="cuber netis => Kubernetes&#10;planner fit => PlannerFit&#10;super base => Supabase">${esc(cfg.correcoes)}</textarea>
          <span class="ajuda"><code>errado => certo</code>, um por linha. Trocado no texto antes de salvar; vale palavra inteira.</span></label>
        <label>Sobre você e suas reuniões
          <textarea name="contexto" rows="7" placeholder="Sou o Leonardo, desenvolvedor. A Bê é minha sócia. Nas reuniões falamos do PlannerFit, um app de agenda pra professores, feito em Flutter + Supabase.">${esc(cfg.contexto)}</textarea>
          <span class="ajuda">Texto livre que o modelo lê antes de explicar e de escrever a ata.</span></label>
      </div>
    </fieldset>

    <fieldset>
      <legend>Cérebro (explicações)</legend>
      <div class="opcoes">
        <label><input type="radio" name="cerebro" value="claude" ${cfg.cerebro === "claude" ? "checked" : ""}>
          <span><b>Claude</b><span class="ajuda">Na nuvem, mais inteligente. Só o texto sai do PC.</span></span></label>
        <label><input type="radio" name="cerebro" value="local" ${cfg.cerebro === "local" ? "checked" : ""}>
          <span><b>Local (Ollama)</b><span class="ajuda">100% no seu PC, grátis, mais simples.</span></span></label>
      </div>
      <div class="grupo-claude">
        <label>Modelo do Claude <select name="modelo_claude">${opcoes(MODELOS_CLAUDE, cfg.modelo_claude)}</select></label>
        <label>Esforço de raciocínio <select name="esforco">${opcoes([["low", "Baixo — mais rápido"], ["medium", "Médio"], ["high", "Alto — mais lento"]], cfg.esforco)}</select></label>
        <label>Chave da API Anthropic
          <span class="chave"><input type="password" name="anthropic_key" placeholder="${cfg.anthropic_key_definida ? "•••••••• configurada" : "sk-ant-…"}" autocomplete="off">
          ${cfg.anthropic_key_definida ? `<button type="button" class="btn" data-apagar="anthropic_key">Remover</button>` : ""}</span>
          <span class="ajuda">Crie em platform.claude.com → API Keys. Fica salva só neste PC.</span></label>
      </div>
      <div class="grupo-local">
        <label>Modelo local <select name="modelo_local">${opcoes(locais.map((m) => [m, m]), cfg.modelo_local)}</select>
          <span class="ajuda">Pra instalar outro: <code>ollama pull nome</code>. Em placa de 8 GB, prefira modelos de ~4B.</span></label>
        <label>Rodar em <select name="local_dispositivo">${opcoes(DISPOSITIVOS, cfg.local_dispositivo)}</select>
          <span class="ajuda">Na CPU cada explicação leva ~45 s (gemma3:4b num Ryzen 7 2700). Use intervalo de 2–5 min.</span></label>
      </div>
    </fieldset>

    <fieldset>
      <legend>Transcrição</legend>
      <div class="opcoes">
        <label><input type="radio" name="transcricao" value="whisper" ${cfg.transcricao === "whisper" ? "checked" : ""}>
          <span><b>Whisper</b><span class="ajuda">Local na GPU, grátis. Outros aparecem juntos.</span></span></label>
        <label><input type="radio" name="transcricao" value="deepgram" ${cfg.transcricao === "deepgram" ? "checked" : ""}>
          <span><b>Deepgram</b><span class="ajuda">Nuvem, pago. Separa “Pessoa 1”, “Pessoa 2”…</span></span></label>
      </div>
      <div class="grupo-whisper">
        <label>Modelo do Whisper <select name="whisper_modelo">${opcoes([["large-v3-turbo", "large-v3-turbo — melhor"], ["medium", "medium"], ["small", "small — mais leve"], ["base", "base — o mais leve"]], cfg.whisper_modelo)}</select></label>
        <label>Rodar em <select name="whisper_dispositivo">${opcoes(DISPOSITIVOS, cfg.whisper_dispositivo)}</select>
          <span class="ajuda cpu-aviso">Na CPU, use <b>small</b> ou <b>base</b>: os maiores ficam mais lentos que a fala e a transcrição vai atrasando.</span></label>
        <label>Sensibilidade (energia mínima pra contar como fala)
          <input type="number" name="limiar" min="50" max="2000" step="50" value="${cfg.limiar}">
          <span class="ajuda">Suba se ruído de fundo virar texto; desça se falas baixas sumirem. Padrão: 250.</span></label>
      </div>
      <div class="grupo-deepgram">
        <label>Chave do Deepgram
          <span class="chave"><input type="password" name="deepgram_key" placeholder="${cfg.deepgram_key_definida ? "•••••••• configurada" : "chave do console.deepgram.com"}" autocomplete="off">
          ${cfg.deepgram_key_definida ? `<button type="button" class="btn" data-apagar="deepgram_key">Remover</button>` : ""}</span></label>
      </div>
    </fieldset>

    <fieldset>
      <legend>Áudio</legend>
      <label class="check"><input type="checkbox" name="usar_mic" ${cfg.usar_mic ? "checked" : ""}> Capturar meu microfone</label>
      <label>Microfone <select name="mic_index">${opcoes([["", "Padrão do Windows"], ...disp.mics.map((d) => [d.index, d.nome])], cfg.mic_index ?? "")}</select></label>
      <label>Áudio dos outros (saída) <select name="saida_index">${opcoes([["", "Saída padrão do Windows"], ...disp.saidas.map((d) => [d.index, d.nome])], cfg.saida_index ?? "")}</select>
        <span class="ajuda">Onde você ouve a reunião — normalmente o fone.</span></label>
      <span class="ajuda">Use fone: com caixa de som, o microfone também capta os outros.</span>
    </fieldset>

    <fieldset>
      <legend>Copiloto</legend>
      <label>Explicar automaticamente <select name="intervalo">${opcoes([[30, "a cada 30 s"], [60, "a cada 1 min"], [120, "a cada 2 min"], [300, "a cada 5 min"], [0, "só quando eu pedir"]], cfg.intervalo)}</select></label>
    </fieldset>`;

  const form = $("#form-ajustes");
  const alternar = () => {
    const c = $("input[name=cerebro]:checked", form)?.value;
    const t = $("input[name=transcricao]:checked", form)?.value;
    $(".grupo-claude", form).classList.toggle("escondido", c !== "claude");
    $(".grupo-local", form).classList.toggle("escondido", c !== "local");
    $(".grupo-whisper", form).classList.toggle("escondido", t !== "whisper");
    $(".grupo-deepgram", form).classList.toggle("escondido", t !== "deepgram");
  };
  $$("input[type=radio]", form).forEach((r) => r.addEventListener("change", alternar));
  alternar();
  $$("[data-apagar]", form).forEach((b) => b.addEventListener("click", () =>
    tentar(async () => { await api("/api/config/" + b.dataset.apagar, { method: "DELETE" }); toast("Chave removida."); carregarAjustes(); })));
}

$("#btn-salvar").addEventListener("click", () => tentar(async () => {
  const f = $("#form-ajustes");
  const v = (n) => f.elements[n]?.value;
  // Windows identifica dispositivos por número; Linux, por nome (texto)
  const indice = (n) => {
    const x = v(n);
    if (x === "" || x === undefined) return null;
    return /^\d+$/.test(x) ? +x : x;
  };
  await api("/api/config", {
    method: "PUT",
    body: {
      vocabulario: v("vocabulario"),
      correcoes: v("correcoes"),
      contexto: v("contexto"),
      cerebro: $("input[name=cerebro]:checked", f).value,
      modelo_claude: v("modelo_claude"),
      esforco: v("esforco"),
      anthropic_key: v("anthropic_key"),
      modelo_local: v("modelo_local"),
      transcricao: $("input[name=transcricao]:checked", f).value,
      whisper_modelo: v("whisper_modelo"),
      whisper_dispositivo: v("whisper_dispositivo"),
      local_dispositivo: v("local_dispositivo"),
      limiar: +v("limiar"),
      deepgram_key: v("deepgram_key"),
      usar_mic: f.elements.usar_mic.checked,
      mic_index: indice("mic_index"),
      saida_index: indice("saida_index"),
      intervalo: +v("intervalo"),
    },
  });
  toast("Ajustes salvos. Valem a partir da próxima reunião.");
  if (!ativa()) $("#intervalo").value = v("intervalo");
  carregarAjustes();
}));

// --- início ----------------------------------------------------------------------

(async () => {
  conectar();
  const cfg = await api("/api/config").catch(() => null);
  if (cfg) $("#intervalo").value = String(cfg.intervalo);
})();
