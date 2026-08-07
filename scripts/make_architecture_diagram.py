#!/usr/bin/env python3
"""Gera o diagrama didático da plataforma (visão de negócio) em .excalidraw.

    python3 scripts/make_architecture_diagram.py

Público: quem precisa entender o que a plataforma decide e o que ela custou, sem abrir código.
O par técnico é `make_technical_diagram.py`.

O diagrama é versionado como *fonte gerada*, não desenhado à mão, pelo mesmo motivo que a política
de domínios do Unity Catalog é criada por script: artefato que só existe porque alguém montou uma
vez não pode ser refeito, revisado em diff, nem reproduzido por outra pessoa. Aqui o texto fica no
código, o diff é legível, a diagramação é recalculada e `Canvas.audit()` reprova colisão, texto
estourando a caixa e seta cruzando título — coisas que revisão a olho não pega.
"""

from __future__ import annotations

from pathlib import Path

from excalidraw_kit import (
    BG_BLUE,
    BG_GREEN,
    BG_RED,
    BG_VIOLET,
    BG_YELLOW,
    BLUE,
    GREEN,
    GREY,
    ORANGE,
    RED,
    VIOLET,
    Canvas,
)

OUT = Path(__file__).resolve().parents[1] / "docs/diagrams/arquitetura-plataforma.excalidraw"
c = Canvas(seed=20260807)

c.label(70, 40, "Plataforma de dados — Northwind Grocers (rede de supermercados)", 32)
c.label(
    74,
    92,
    "Como a compra de uma pessoa vira decisão de negócio — e o que aceitamos perder pelo caminho.",
    18,
    GREY,
)

# ---------------------------------------------------------------------------
# 1 · POR QUE ELA EXISTE
# ---------------------------------------------------------------------------
y: float = 168
c.label(70, y, "1 · POR QUE ELA EXISTE", 24, VIOLET)
c.note(
    70,
    y + 34,
    900,
    "Nada foi construído porque a tecnologia era interessante. Tudo nasce destas cinco decisões — "
    "cada uma com um dono, um prazo e um custo de errar.",
    GREY,
)
decisoes = c.stacker([70, 366, 662, 958, 1254], y + 94)
for i, (titulo, corpo) in enumerate(
    [
        (
            "D1 · Cupom certo, pessoa certa",
            "Quem recebe qual cupom na próxima campanha.\nDono: Marketing · Prazo: dia seguinte\n"
            "Errar = pagar desconto para quem já ia comprar.",
        ),
        (
            "D2 · Cliente escorregando",
            "Quem está comprando cada vez menos, antes de sumir.\nDono: CRM · Prazo: semanal\n"
            "Errar = perda que só aparece quando não dá mais para reverter.",
        ),
        (
            "D3 · Promoção furada",
            "Derrubar uma promoção no meio se estiver queimando verba.\nDono: Trade · Prazo: minutos\n"
            "Errar = 2 semanas de orçamento gastas antes do relatório sair.",
        ),
        (
            "D4 · Loja fora do padrão",
            "Venda estranha hoje: preço errado, ruptura, furto.\nDono: Operações · Prazo: minutos\n"
            "Errar = perda que cresce a cada hora parada.",
        ),
        (
            "D5 · Pergunta em português",
            "O time comercial pergunta e recebe resposta sem abrir chamado.\n"
            "Dono: Comercial · Prazo: na hora\nErrar = o analista vira gargalo de toda dúvida.",
        ),
    ]
):
    decisoes.put(i, 272, titulo, corpo, VIOLET, BG_VIOLET)
dec_bottom = decisoes.bottom()
c.frame(46, y + 84, 1500, dec_bottom - (y + 84) + 26)
c.note(
    70,
    dec_bottom + 46,
    1000,
    "▲ Só D3 e D4 justificam tempo real. Usar streaming onde lote resolve é desperdício de "
    "dinheiro; usar lote onde minutos importam é prejuízo de negócio. A fronteira é uma decisão, "
    "não um gosto.",
    RED,
)

# ---------------------------------------------------------------------------
# 2 · DE ONDE VEM O DADO
# ---------------------------------------------------------------------------
y = dec_bottom + 130
c.label(70, y, "2 · DE ONDE VEM O DADO", 24, VIOLET)
fontes = c.stacker([620, 1060], y + 48)
f1 = fontes.put(
    0,
    400,
    "Compras de verdade",
    "Histórico real e anonimizado de 2.500 famílias: 2,6 milhões de itens de cupom fiscal, "
    "582 lojas, 2 anos. Traz comportamento de gente de verdade — que ninguém consegue inventar.",
    BLUE,
    BG_BLUE,
)
f2 = fontes.put(
    1,
    400,
    "Amplificador (gerador)",
    "Sorteia cestas reais para gerar volume — e injeta defeitos de propósito: evento atrasado, "
    "entrega repetida, coluna que muda de nome ou de tipo no meio do caminho.",
    BLUE,
    BG_BLUE,
)
c.note(
    150,
    y + 56,
    420,
    "Por que estragar o dado de propósito?\nPorque pipeline que só roda no caminho feliz não prova "
    "nada. A graça é ver a plataforma perceber o problema em vez de espalhá-lo.",
    RED,
)
fontes_bottom = fontes.bottom()
c.frame(600, y + 36, 940, fontes_bottom - (y + 36) + 26)

# ---------------------------------------------------------------------------
# 3 · O CAMINHO DO DADO
# ---------------------------------------------------------------------------
y = fontes_bottom + 96
c.label(70, y, "3 · O CAMINHO DO DADO", 24, VIOLET)
c.note(70, y + 34, 250, "cru → limpo → pronto para decidir", GREY)

camadas = c.stacker([690], y + 86, gap=96)
c1 = camadas.put(
    0,
    500,
    "BRONZE — cru, do jeito que chegou",
    "Nada é descartado. Campo que não encaixa no formato vai para uma coluna de resgate em vez de "
    "sumir. Cada linha guarda de qual arquivo veio e quando entrou.",
    BLUE,
    BG_BLUE,
)
c2 = camadas.put(
    0,
    500,
    "PRATA — limpo e padronizado",
    "Regras de qualidade escritas e versionadas. Linha reprovada vai para a quarentena com o "
    "motivo registrado — nunca é apagada em silêncio. Guarda o histórico de produto e de família "
    "com data de início e fim.",
    BLUE,
    BG_BLUE,
)
c3 = camadas.put(
    0,
    500,
    "OURO — pronto para decidir",
    "Modelo estrela: um fato de item comprado, mais resumos por loja/dia, por promoção e por "
    "família. É daqui que sai todo painel, todo modelo e toda resposta do assistente.",
    BLUE,
    BG_BLUE,
)
c.arrow(f1, c1)
c.arrow(f2, c1)
c.arrow(c1, c2)
c.arrow(c2, c3)

c.note(
    140,
    c1["y"] + 6,
    440,
    "✔ Provado de verdade: quando o fornecedor trocou o tipo de um campo no meio do dia, 49.468 "
    "linhas foram resgatadas e o total de linhas ficou intacto. Nada sumiu calado.",
    GREEN,
)
c.note(
    140,
    c2["y"] + 6,
    440,
    "✘ Achado que dói: juntar o fato com o histórico sem respeitar a data de validade inflava a "
    "receita em 1,706%. O número ficava bonito e errado — hoje um teste impede.",
    RED,
)
c.note(
    140,
    c3["y"] + 6,
    440,
    "✔ Ouro fecha com prata: 198.013 linhas e R$ 613.396,36, diferença zero. Conferido a cada "
    "execução, não uma vez só.",
    GREEN,
)
camadas_bottom = camadas.bottom()
c.frame(110, y + 74, 1110, camadas_bottom - (y + 74) + 26)

# ---------------------------------------------------------------------------
# 4 · O QUE AS PESSOAS REALMENTE USAM
# ---------------------------------------------------------------------------
y = camadas_bottom + 110
c.label(70, y, "4 · O QUE AS PESSOAS REALMENTE USAM", 24, VIOLET)
COLS = [70, 430, 790, 1150]
produtos = c.stacker(COLS, y + 52)
p0 = produtos.put(
    0,
    330,
    "Números oficiais (12 indicadores)",
    "Venda, cestas, desconto, clientes sumindo. Cada número tem UMA definição só, guardada no "
    "catálogo. Quem consome não escreve a própria conta — o motor recalcula no recorte pedido.",
    GREEN,
    BG_GREEN,
)
p1 = produtos.put(
    1,
    330,
    "Cada tabela com um dono (5 domínios)",
    "Marketing, Trade, Operações, Base Comercial e Plataforma. O catálogo recusa um dono que não "
    "esteja na lista — é regra, não etiqueta livre.",
    GREEN,
    BG_GREEN,
)
p2 = produtos.put(
    2,
    330,
    "Modelo de risco de sumiço",
    "REPROVADO no portão de qualidade — e continua reprovado. Perdeu para uma régua simples de "
    "'quem comprou mais recentemente'. Publicar mesmo assim seria vender fumaça.",
    RED,
    BG_RED,
)
produtos.put(
    3,
    330,
    "Assistente que responde",
    "Responde em português usando só dado governado, e diz 'não tenho essa informação' em vez de "
    "inventar um número. Avaliado por juiz automático antes de subir.",
    GREEN,
    BG_GREEN,
)
c.arrow(c3, p2)

volta = c.card(
    430,
    produtos.bottom() + 56,
    560,
    "↩ E volta para a operação",
    "A lista de cupons e a nota de risco de cada família são gravadas de volta no sistema que a "
    "loja e o marketing já usam. Ninguém precisa abrir a plataforma para agir.",
    VIOLET,
    BG_VIOLET,
)
c.arrow(p1, volta, VIOLET)
c.arrow(p2, volta, VIOLET)
c.note(
    1030,
    volta["y"] + 8,
    440,
    "▲ Aqui está a diferença: plataforma que só gera painel é relatório. Plataforma que devolve a "
    "decisão para quem opera é infraestrutura — é o que torna D1 e D2 acionáveis, não só visíveis.",
    GREY,
)
prod_bottom = volta["y"] + volta["height"]
c.frame(46, y + 38, 1460, prod_bottom - (y + 38) + 26)

# ---------------------------------------------------------------------------
# 5 · O QUE ACEITAMOS PERDER
# ---------------------------------------------------------------------------
y = prod_bottom + 110
c.label(70, y, "5 · O QUE ACEITAMOS PERDER (e por quê)", 24, VIOLET)
c.note(
    70,
    y + 34,
    980,
    "Rodamos na versão gratuita do Databricks. Lacuna escrita é lacuna honesta; lacuna escondida é "
    "mentira. Cada uma abaixo tem o custo medido, não estimado.",
    GREY,
)
tradeoffs = c.stacker(COLS, y + 92)
for i, (titulo, corpo) in enumerate(
    [
        (
            "Um workspace só",
            "Sem plano pago não dá para separar os ambientes em contas diferentes. Separamos em três "
            "áreas do catálogo: desenvolvimento, teste e produção. Separação real, porém por "
            "convenção — quem é administrador alcança todas.",
        ),
        (
            "Uma esteira só",
            "As três camadas dividem o mesmo processamento. Consequência honesta: um erro na última "
            "camada derruba também a entrada de dados. Numa conta paga seriam duas esteiras "
            "separadas.",
        ),
        (
            "Reprodutibilidade: dispensada",
            "Provar que a mesma entrada gera o mesmo resultado exigiria reprocessar tudo — e "
            "estourar a cota derruba o ambiente inteiro pelo resto do dia. Custo medido, lacuna "
            "registrada.",
        ),
        (
            "Sem tela de diagnóstico",
            "A tela que mostra onde o processamento trava não existe neste plano. Toda otimização "
            "virou mudança de código, com número antes e depois — nunca botão que ninguém "
            "consegue conferir.",
        ),
    ]
):
    tradeoffs.put(i, 330, titulo, corpo, ORANGE, BG_YELLOW, fill_style="hachure")
tr_bottom = tradeoffs.bottom()
c.frame(46, y + 80, 1460, tr_bottom - (y + 80) + 26)

# ---------------------------------------------------------------------------
# 6 · O QUE IMPEDE ISSO DE APODRECER
# ---------------------------------------------------------------------------
y = tr_bottom + 100
c.label(70, y, "6 · O QUE IMPEDE ISSO DE APODRECER", 24, VIOLET)
disciplina = c.stacker(COLS, y + 52)
for i, (titulo, corpo) in enumerate(
    [
        (
            "53 requisitos numerados",
            "26 provados por teste automático, 1 dispensado com custo medido. Requisito sem teste "
            "trava a entrega — quem cobra é a máquina, não a disciplina de ninguém.",
        ),
        (
            "Todo número vem de medição",
            "Afirmação sem medida não entra na documentação. Seis vezes o instrumento estava "
            "quebrado e o resultado parecia perfeito — por isso a régua é sempre conferida contra "
            "a fonte.",
        ),
        (
            "Só sobe o que passou",
            "Produção recebe exatamente a mesma versão que passou no ambiente de teste — conferido "
            "no registro do próprio servidor, não na promessa de quem publicou.",
        ),
        (
            "Erro fica visível",
            "Decisão revertida continua no histórico com o texto original. Apagar o engano destrói "
            "a parte mais útil do registro: o formato do erro.",
        ),
    ]
):
    disciplina.put(i, 330, titulo, corpo, GREEN, BG_GREEN)
fim = disciplina.bottom()
c.frame(46, y + 38, 1460, fim - (y + 38) + 26)

problems = c.audit()
for problem in problems:
    print("AUDIT:", problem)
c.save(OUT)
print(f"{OUT.name}: {len(c.elements)} elementos · {len(problems)} problema(s) de layout")
