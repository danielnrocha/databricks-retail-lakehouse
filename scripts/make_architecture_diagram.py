#!/usr/bin/env python3
"""Gera o diagrama didático da plataforma em formato .excalidraw nativo.

    python3 scripts/make_architecture_diagram.py

O diagrama é versionado como *fonte gerada*, não desenhado à mão, pelo mesmo motivo que a política
de domínios do Unity Catalog é criada por script: artefato que só existe porque alguém montou uma
vez é artefato que ninguém consegue refazer nem revisar. Aqui o texto fica no código, o diff é
legível e a diagramação é recalculada.

Escolhas de estilo, todas nativas do Excalidraw:

* `fontFamily: 5` (Excalifont) e `roughness: 1` — o traço desenhado à mão padrão.
* Paleta oficial do Excalidraw (#1971c2, #2f9e44, #e03131, #f08c00, #6741d9 e seus fundos claros).
* Altura de cada caixa calculada a partir do texto quebrado, com 8px de margem — sem sobra vazia.
  A largura de caractere é estimada por baixo de propósito: se a estimativa errar, o Excalidraw
  cresce a caixa ao abrir e o encaixe continua justo. Errar para cima deixaria buraco.
* Posição, ângulo e semente de cada elemento recebem ruído pequeno e determinístico, para o
  desenho não parecer saída de gerador — que é exatamente o que ele é.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

RNG = random.Random(20260807)
NOW = 1786150000000

FONT = 5  # Excalifont
LH = 1.25
INK = "#1e1e1e"
BLUE, GREEN, RED, YELLOW, VIOLET = "#1971c2", "#2f9e44", "#e03131", "#f08c00", "#6741d9"
BG_BLUE, BG_GREEN, BG_RED, BG_YELLOW, BG_VIOLET = (
    "#a5d8ff",
    "#b2f2bb",
    "#ffc9c9",
    "#ffec99",
    "#d0bfff",
)
PAD = 8

Element = dict[str, Any]

elements: list[Element] = []

OUT = Path(__file__).resolve().parents[1] / "docs/diagrams/arquitetura-plataforma.excalidraw"


def seed() -> int:
    return RNG.randint(1, 2**31 - 1)


def base(kind: str, x: float, y: float, w: float, h: float, **kw: Any) -> Element:
    el: Element = {
        "id": f"el{len(elements):03d}{RNG.randint(100, 999)}",
        "type": kind,
        "x": round(x, 2),
        "y": round(y, 2),
        "width": round(w, 2),
        "height": round(h, 2),
        "angle": 0,
        "strokeColor": INK,
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 1,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": {"type": 3},
        "seed": seed(),
        "version": RNG.randint(20, 400),
        "versionNonce": seed(),
        "isDeleted": False,
        "boundElements": [],
        "updated": NOW,
        "link": None,
        "locked": False,
    }
    el.update(kw)
    return el


# --- medicao de texto -------------------------------------------------------
def char_w(size: int) -> float:
    return size * 0.5


def wrap(text: str, size: int, max_px: float) -> list[str]:
    limit = max(8, int(max_px / char_w(size)))
    out: list[str] = []
    for para in text.split("\n"):
        if not para:
            out.append("")
            continue
        line = ""
        for word in para.split(" "):
            trial = f"{line} {word}".strip()
            if len(trial) <= limit:
                line = trial
            else:
                if line:
                    out.append(line)
                line = word
        out.append(line)
    return out


def text_size(lines: list[str], size: int) -> tuple[float, float]:
    w = max((len(ln) for ln in lines), default=1) * char_w(size)
    return w, len(lines) * size * LH


def label(
    x: float, y: float, txt: str, size: int = 20, color: str = INK, align: str = "left"
) -> Element:
    lines = txt.split("\n")
    w, h = text_size(lines, size)
    el = base(
        "text",
        x,
        y,
        w,
        h,
        strokeColor=color,
        roundness=None,
        text=txt,
        originalText=txt,
        fontSize=size,
        fontFamily=FONT,
        textAlign=align,
        verticalAlign="top",
        containerId=None,
        lineHeight=LH,
        autoResize=True,
    )
    elements.append(el)
    return el


def card(
    x: float,
    y: float,
    w: float,
    title: str,
    body: str,
    stroke: str = INK,
    fill: str = "transparent",
    size: int = 16,
    fill_style: str = "solid",
    tilt: float = 0.0,
) -> Element:
    """Caixa com texto embutido, altura ajustada ao conteudo (sem sobra)."""
    inner = w - 2 * PAD
    lines = [*wrap(title, size, inner), "", *wrap(body, size, inner)]
    _, th = text_size(lines, size)
    h = th + 2 * PAD

    jx = x + RNG.uniform(-5, 5)
    jy = y + RNG.uniform(-4, 4)
    angle = tilt if tilt else RNG.uniform(-0.009, 0.009)

    box = base(
        "rectangle",
        jx,
        jy,
        w,
        h,
        strokeColor=stroke,
        backgroundColor=fill,
        fillStyle=fill_style,
        strokeWidth=RNG.choice([1, 1, 2]),
        angle=round(angle, 4),
    )
    txt_raw = "\n".join(lines)
    txt = base(
        "text",
        jx + PAD,
        jy + PAD,
        inner,
        th,
        roundness=None,
        angle=round(angle, 4),
        text=txt_raw,
        originalText=txt_raw,
        fontSize=size,
        fontFamily=FONT,
        textAlign="left",
        verticalAlign="middle",
        containerId=box["id"],
        lineHeight=LH,
        autoResize=False,
    )
    box["boundElements"] = [{"type": "text", "id": txt["id"]}]
    elements.append(box)
    elements.append(txt)
    return box


def note(x: float, y: float, w: float, txt: str, color: str = RED, size: int = 15) -> Element:
    lines = wrap(txt, size, w)
    tw, th = text_size(lines, size)
    body = "\n".join(lines)
    el = base(
        "text",
        x,
        y,
        tw,
        th,
        strokeColor=color,
        roundness=None,
        angle=round(RNG.uniform(-0.012, 0.012), 4),
        text=body,
        originalText=body,
        fontSize=size,
        fontFamily=FONT,
        textAlign="left",
        verticalAlign="top",
        containerId=None,
        lineHeight=LH,
        autoResize=True,
    )
    elements.append(el)
    return el


def group_frame(x: float, y: float, w: float, h: float, color: str = "#adb5bd") -> Element:
    el = base(
        "rectangle",
        x,
        y,
        w,
        h,
        strokeColor=color,
        strokeStyle="dashed",
        strokeWidth=1,
        backgroundColor="transparent",
        angle=round(RNG.uniform(-0.004, 0.004), 4),
    )
    elements.insert(0, el)
    return el


def arrow(
    a: Element, b: Element, color: str = INK, dashed: bool = False, bow: float = 0.0
) -> Element:
    ax, ay = a["x"] + a["width"] / 2, a["y"] + a["height"]
    bx, by = b["x"] + b["width"] / 2, b["y"]
    gap = 6
    ay += gap
    by -= gap
    dx, dy = bx - ax, by - ay
    mid = [dx / 2 + bow + RNG.uniform(-6, 6), dy / 2 + RNG.uniform(-4, 4)]
    el = base(
        "arrow",
        ax,
        ay,
        dx,
        dy,
        strokeColor=color,
        strokeWidth=2,
        strokeStyle="dashed" if dashed else "solid",
        roundness={"type": 2},
        points=[[0, 0], [round(mid[0], 2), round(mid[1], 2)], [round(dx, 2), round(dy, 2)]],
        lastCommittedPoint=None,
        startBinding={"elementId": a["id"], "focus": round(RNG.uniform(-0.1, 0.1), 3), "gap": gap},
        endBinding={"elementId": b["id"], "focus": round(RNG.uniform(-0.1, 0.1), 3), "gap": gap},
        startArrowhead=None,
        endArrowhead="arrow",
        elbowed=False,
    )
    a.setdefault("boundElements", []).append({"id": el["id"], "type": "arrow"})
    b.setdefault("boundElements", []).append({"id": el["id"], "type": "arrow"})
    elements.append(el)
    return el


# ============================================================================
# CONTEUDO
# ============================================================================
label(70, 40, "Plataforma de dados — Northwind Grocers (rede de supermercados)", 32)
label(
    74,
    92,
    "Como a compra de uma pessoa vira decisão de negócio — e o que aceitamos perder pelo caminho.",
    18,
    "#495057",
)

# --- 1. decisoes ------------------------------------------------------------
label(70, 168, "1 · POR QUE ELA EXISTE", 24, VIOLET)
note(
    70,
    202,
    900,
    "Nada foi construído porque a tecnologia era interessante. Tudo nasce destas cinco decisões — "
    "cada uma com um dono, um prazo e um custo de errar.",
    "#495057",
)

dec = [
    (
        "D1 · Cupom certo, pessoa certa",
        "Quem recebe qual cupom na próxima campanha.\nDono: Marketing · Prazo: dia seguinte\nErrar = pagar desconto para quem já ia comprar.",
    ),
    (
        "D2 · Cliente escorregando",
        "Quem está comprando cada vez menos, antes de sumir.\nDono: CRM · Prazo: semanal\nErrar = perda que só aparece quando não dá mais para reverter.",
    ),
    (
        "D3 · Promoção furada",
        "Derrubar uma promoção no meio se estiver queimando verba.\nDono: Trade · Prazo: minutos\nErrar = 2 semanas de orçamento gastas antes do relatório sair.",
    ),
    (
        "D4 · Loja fora do padrão",
        "Venda estranha hoje: preço errado, ruptura, furto.\nDono: Operações · Prazo: minutos\nErrar = perda que cresce a cada hora parada.",
    ),
    (
        "D5 · Pergunta em português",
        "O time comercial pergunta e recebe resposta sem abrir chamado.\nDono: Comercial · Prazo: na hora\nErrar = o analista vira gargalo de toda dúvida.",
    ),
]
dec_cards = []
xs = [70, 366, 662, 958, 1254]
for (t, b), x in zip(dec, xs, strict=True):
    dec_cards.append(card(x, 262, 272, t, b, VIOLET, BG_VIOLET, 15))

bottom1 = max(c["y"] + c["height"] for c in dec_cards)
note(
    70,
    bottom1 + 46,
    1000,
    "▲ Só D3 e D4 justificam tempo real. Usar streaming onde lote resolve é desperdício de dinheiro; "
    "usar lote onde minutos importam é prejuízo de negócio. A fronteira é uma decisão, não um gosto.",
    RED,
)

# --- 2. fontes --------------------------------------------------------------
y2 = bottom1 + 130
label(70, y2, "2 · DE ONDE VEM O DADO", 24, VIOLET)
f1 = card(
    250,
    y2 + 48,
    400,
    "Compras de verdade",
    "Histórico real e anonimizado de 2.500 famílias: 2,6 milhões de itens de cupom fiscal, "
    "582 lojas, 2 anos. Traz comportamento de gente de verdade — que ninguém consegue inventar.",
    BLUE,
    BG_BLUE,
)
f2 = card(
    690,
    y2 + 48,
    400,
    "Amplificador (gerador)",
    "Sorteia cestas reais para gerar volume — e injeta defeitos de propósito: evento atrasado, "
    "entrega repetida, coluna que muda de nome ou de tipo no meio do caminho.",
    BLUE,
    BG_BLUE,
)
note(
    1130,
    y2 + 56,
    380,
    "Por que estragar o dado de propósito?\nPorque pipeline que só roda no caminho feliz não prova "
    "nada. A graça é ver a plataforma perceber o problema em vez de espalhá-lo.",
    RED,
)

# --- 3. camadas -------------------------------------------------------------
y3 = max(f1["y"] + f1["height"], f2["y"] + f2["height"]) + 96
label(70, y3, "3 · O CAMINHO DO DADO", 24, VIOLET)
note(70, y3 + 34, 250, "cru → limpo → pronto para decidir", "#495057")

c1 = card(
    380,
    y3 + 86,
    500,
    "BRONZE — cru, do jeito que chegou",
    "Nada é descartado. Campo que não encaixa no formato vai para uma coluna de resgate em vez de "
    "sumir. Cada linha guarda de qual arquivo veio e quando entrou.",
    BLUE,
    BG_BLUE,
)
c2 = card(
    380,
    c1["y"] + c1["height"] + 96,
    500,
    "PRATA — limpo e padronizado",
    "Regras de qualidade escritas e versionadas. Linha reprovada vai para a quarentena com o motivo "
    "registrado — nunca é apagada em silêncio. Guarda o histórico de produto e de família com "
    "data de início e fim.",
    BLUE,
    BG_BLUE,
)
c3 = card(
    380,
    c2["y"] + c2["height"] + 96,
    500,
    "OURO — pronto para decidir",
    "Modelo estrela: um fato de item comprado, mais resumos por loja/dia, por promoção e por "
    "família. É daqui que sai todo painel, todo modelo e toda resposta do assistente.",
    BLUE,
    BG_BLUE,
)
arrow(f1, c1)
arrow(f2, c1)
arrow(c1, c2)
arrow(c2, c3)

note(
    930,
    c1["y"] + 6,
    430,
    "✔ Provado de verdade: quando o fornecedor trocou o tipo de um campo no meio do dia, 49.468 "
    "linhas foram resgatadas e o total de linhas ficou intacto. Nada sumiu calado.",
    GREEN,
)
note(
    930,
    c2["y"] + 6,
    430,
    "✘ Achado que dói: juntar o fato com o histórico sem respeitar a data de validade inflava a "
    "receita em 1,706%. O número ficava bonito e errado — hoje um teste impede.",
    RED,
)
note(
    930,
    c3["y"] + 6,
    430,
    "✔ Ouro fecha com prata: 198.013 linhas e R$ 613.396,36, diferença zero. Conferido a cada "
    "execução, não uma vez só.",
    GREEN,
)

# --- 4. produtos de dados ---------------------------------------------------
y4 = c3["y"] + c3["height"] + 110
label(70, y4, "4 · O QUE AS PESSOAS REALMENTE USAM", 24, VIOLET)
p_cards = []
prod = [
    (
        "Números oficiais (12 indicadores)",
        "Venda, cestas, desconto, clientes sumindo. Cada número tem UMA definição só, guardada no "
        "catálogo. Quem consome não escreve a própria conta — o motor recalcula no recorte pedido.",
    ),
    (
        "Cada tabela com um dono (5 domínios)",
        "Marketing, Trade, Operações, Base Comercial e Plataforma. O catálogo recusa um dono que "
        "não esteja na lista — é regra, não etiqueta livre.",
    ),
    (
        "Modelo de risco de sumiço",
        "REPROVADO no portão de qualidade — e continua reprovado. Perdeu para uma régua simples "
        "de 'quem comprou mais recentemente'. Publicar mesmo assim seria vender fumaça.",
    ),
    (
        "Assistente que responde",
        "Responde em português usando só dado governado, e diz 'não tenho essa informação' em vez "
        "de inventar um número. Avaliado por juiz automático antes de subir.",
    ),
]
pxs = [70, 430, 790, 1150]
for (t, b), x in zip(prod, pxs, strict=True):
    stroke, fill = (RED, BG_RED) if t.startswith("Modelo") else (GREEN, BG_GREEN)
    p_cards.append(card(x, y4 + 52, 330, t, b, stroke, fill, 15))
arrow(c3, p_cards[1], bow=-40)

p_bottom = max(c["y"] + c["height"] for c in p_cards)
volta = card(
    430,
    p_bottom + 56,
    560,
    "↩ E volta para a operação",
    "A lista de cupons e a nota de risco de cada família são gravadas de volta no sistema que a "
    "loja e o marketing já usam. Ninguém precisa abrir a plataforma para agir.",
    VIOLET,
    BG_VIOLET,
    15,
)
arrow(p_cards[1], volta, VIOLET)
arrow(p_cards[2], volta, VIOLET)
note(
    1030,
    volta["y"] + 8,
    440,
    "▲ Aqui está a diferença: plataforma que só gera painel é relatório. Plataforma que devolve a "
    "decisão para quem opera é infraestrutura — é o que torna D1 e D2 acionáveis, não só visíveis.",
    "#495057",
)
y4b = volta["y"] + volta["height"]

# --- 5. trade-offs ----------------------------------------------------------
y5 = y4b + 110
label(70, y5, "5 · O QUE ACEITAMOS PERDER (e por quê)", 24, VIOLET)
note(
    70,
    y5 + 34,
    980,
    "Rodamos na versão gratuita do Databricks. Lacuna escrita é lacuna honesta; lacuna escondida é "
    "mentira. Cada uma abaixo tem o custo medido, não estimado.",
    "#495057",
)
tr = [
    (
        "Um workspace só",
        "Sem plano pago não dá para separar os ambientes em contas diferentes. Separamos em três "
        "áreas do catálogo: desenvolvimento, teste e produção. Separação real, porém por convenção "
        "— quem é administrador alcança todas.",
    ),
    (
        "Um pipeline por tipo",
        "As três camadas dividem a mesma esteira. Consequência honesta: um erro na última camada "
        "derruba também a entrada de dados. Numa conta paga seriam duas esteiras separadas.",
    ),
    (
        "Reprodutibilidade: dispensada",
        "Provar que a mesma entrada gera o mesmo resultado exigiria reprocessar tudo — e estourar a "
        "cota derruba o ambiente inteiro pelo resto do dia. Custo medido, lacuna registrada.",
    ),
    (
        "Sem tela de diagnóstico",
        "A tela que mostra onde o processamento trava não existe neste plano. Toda otimização virou "
        "mudança de código, com número antes e depois — nunca botão que ninguém consegue conferir.",
    ),
]
t_cards = []
for (t, b), x in zip(tr, pxs, strict=True):
    t_cards.append(card(x, y5 + 92, 330, t, b, YELLOW, BG_YELLOW, 15, fill_style="hachure"))

# --- 6. disciplina ----------------------------------------------------------
y6 = max(c["y"] + c["height"] for c in t_cards) + 100
label(70, y6, "6 · O QUE IMPEDE ISSO DE APODRECER", 24, VIOLET)
disc = [
    (
        "53 requisitos numerados",
        "26 provados por teste automático, 1 dispensado com custo medido. Requisito sem teste trava "
        "a entrega — quem cobra é a máquina, não a disciplina de ninguém.",
    ),
    (
        "Todo número vem de medição",
        "Afirmação sem medida não entra na documentação. Seis vezes o instrumento estava quebrado "
        "e o resultado parecia perfeito — por isso a régua é sempre conferida contra a fonte.",
    ),
    (
        "Só sobe o que passou",
        "Produção recebe exatamente a mesma versão que passou no ambiente de teste — conferido no "
        "registro do próprio servidor, não na promessa de quem publicou.",
    ),
    (
        "Erro fica visível",
        "Decisão revertida continua no histórico com o texto original. Apagar o engano destrói a "
        "parte mais útil do registro: o formato do erro.",
    ),
]
d_cards = []
for (t, b), x in zip(disc, pxs, strict=True):
    d_cards.append(card(x, y6 + 52, 330, t, b, GREEN, BG_GREEN, 15))

end_y = max(c["y"] + c["height"] for c in d_cards)

# molduras de secao
group_frame(46, 246, 1500, bottom1 - 246 + 26)
group_frame(
    210, y2 + 36, 1330, max(f1["y"] + f1["height"], f2["y"] + f2["height"]) - (y2 + 36) + 26
)
group_frame(330, y3 + 74, 1080, (c3["y"] + c3["height"]) - (y3 + 74) + 26)
group_frame(46, y4 + 38, 1460, y4b - (y4 + 38) + 26)
group_frame(46, y5 + 80, 1460, max(c["y"] + c["height"] for c in t_cards) - (y5 + 80) + 26)
group_frame(46, y6 + 38, 1460, end_y - (y6 + 38) + 26)

doc = {
    "type": "excalidraw",
    "version": 2,
    "source": "https://excalidraw.com",
    "elements": elements,
    "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
    "files": {},
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"{OUT.name}: {len(elements)} elementos, altura ~{int(end_y)}px")
