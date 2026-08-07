#!/usr/bin/env python3
"""Gera a referência técnica da plataforma em .excalidraw.

    python3 scripts/make_technical_diagram.py

Público: engenheiro de dados que trabalha com Databricks. Ao contrário do diagrama de negócio, aqui
o jargão é o conteúdo — nome de opção, SQLSTATE, assinatura de API e o número medido ao lado de
cada afirmação. Prosa em Excalifont, literal de código em Cascadia; ambas nativas do Excalidraw.

Regra que vale para cada caixa: se há um número, ele veio de uma medição registrada em
`docs/architecture/*-findings.md` ou em `data/perf/*.json`. Nada aqui é estimado.
"""

from __future__ import annotations

from pathlib import Path

from excalidraw_kit import (
    BG_BLUE,
    BG_GREEN,
    BG_GREY,
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

OUT = Path(__file__).resolve().parents[1] / "docs/diagrams/referencia-tecnica.excalidraw"
c = Canvas(seed=20260807)

COL = [70, 620, 1170, 1720]  # quatro colunas de 500
W = 500

c.label(70, 40, "Retail Lakehouse — referência técnica", 32)
c.label(
    74,
    92,
    "O que foi usado, o que foi medido, e onde a plataforma não entrega o que a documentação promete.",
    18,
    GREY,
)
c.label(
    74, 120, "Databricks Free Edition · serverless · Unity Catalog · 2X-Small + Photon", 16, GREY
)

# ---------------------------------------------------------------------------
# 1 · STACK
# ---------------------------------------------------------------------------
y: float = 190
c.label(70, y, "1 · STACK — o que de fato está em uso", 24, VIOLET)
stack = [
    ("Lakeflow Spark Declarative Pipelines", BLUE, BG_BLUE),
    ("Auto Loader (cloudFiles)", BLUE, BG_BLUE),
    ("AUTO CDC → SCD Type 2", BLUE, BG_BLUE),
    ("Unity Catalog: catálogos, volumes, lineage", BLUE, BG_BLUE),
    ("UC Metric Views", GREEN, BG_GREEN),
    ("UC Tag Policies (Domains)", GREEN, BG_GREEN),
    ("DQX + expectations governadas", GREEN, BG_GREEN),
    ("Liquid clustering + Predictive Optimization", GREEN, BG_GREEN),
    ("MLflow 3: tracing, evaluate, registry, alias", ORANGE, BG_YELLOW),
    ("Model Serving (gpt-oss-120b, tool calling)", ORANGE, BG_YELLOW),
    ("UC Functions como tools do agente", ORANGE, BG_YELLOW),
    ("Databricks Asset Bundles (3 targets)", VIOLET, BG_VIOLET),
    ("Statement Execution API", VIOLET, BG_VIOLET),
    ("system.query.history / information_schema", VIOLET, BG_VIOLET),
]
cx, cy = 70, y + 44
chips = []
for txt, stroke, fill in stack:
    chip = c.chip(cx, cy, txt, stroke, fill)
    if cx + chip["width"] > 2180:
        cx, cy = 70, cy + chip["height"] + 12
        chip["x"], chip["y"] = cx, cy
        for el in c.elements:
            if el.get("containerId") == chip["id"]:
                el["x"], el["y"] = cx + 8, cy + 8
    cx += chip["width"] + 12
    chips.append(chip)
stack_bottom = max(ch["y"] + ch["height"] for ch in chips)
c.frame(52, y + 36, 2160, stack_bottom - (y + 36) + 20)

# ---------------------------------------------------------------------------
# 2 · INGESTÃO
# ---------------------------------------------------------------------------
y = stack_bottom + 70
c.label(70, y, "2 · INGESTÃO — Auto Loader dentro do pipeline declarativo", 24, VIOLET)

s = c.stacker(COL, y + 50)
opts = s.put(
    0,
    W,
    "",
    "cloudFiles.format          = json\n"
    "cloudFiles.schemaLocation  = <landing>/_schema\n"
    "cloudFiles.schemaEvolutionMode = addNewColumns\n"
    "cloudFiles.rescuedDataColumn   = _rescued_data\n"
    "cloudFiles.inferColumnTypes    = true\n"
    "cloudFiles.schemaHints = event_ts TIMESTAMP",
    GREY,
    BG_GREY,
    13,
    mono=True,
)
s.put(
    0,
    W,
    "Colunas de linhagem em 100% das linhas",
    "_source_file · _source_file_ts · _ingest_ts · _pipeline_id. Sem isso, ING-002 é uma promessa "
    "e não uma coluna que dá para consultar.",
    BLUE,
    BG_BLUE,
)
s.put(
    1,
    W,
    "Auto Loader infere de amostra do diretório",
    "Não da ordem de chegada. Consequência: um backfill NUNCA demonstra evolução de schema — a "
    "forma pós-drift vira o schema inicial e nada evolui. A prova exige subir em tranches, com "
    "run entre cada uma.",
    RED,
    BG_RED,
)
s.put(
    1,
    W,
    "Evolução de schema = update CANCELED",
    "O flow termina, o update vai a CANCELED e o Lakeflow inicia um sucessor com cause=SCHEMA_CHANGE. "
    "Orquestração que trata CANCELED como falha alarma exatamente quando o pipeline acertou.",
    RED,
    BG_RED,
)
s.put(
    2,
    W,
    "Medido em 4 tranches de 100 arquivos",
    "T1 pré-drift: 1 versão de schema, 0 resgatadas.\n"
    "T2 coluna nova → 2 versões.\n"
    "T3 rename → 3 versões.\n"
    "T4 retype → 49.468 resgatadas, 200.000 linhas preservadas (400 arq. × 500).\n"
    "Conferido contra a fonte em disco: 49.468 eventos com quantity string. Bate exato.",
    GREEN,
    BG_GREEN,
)
s.put(
    3,
    W,
    "O drift que não erra nem resgata",
    "Rename deixa trans_time E transaction_time na tabela, cada um non-null em metade das linhas. "
    "Nada falha, nada vai para _rescued_data. Query presa ao nome antigo continua devolvendo "
    "linhas e para de cobrir o presente — em silêncio.",
    ORANGE,
    BG_YELLOW,
)
ing_bottom = s.bottom()
c.frame(52, y + 36, 2160, ing_bottom - (y + 36) + 20)

# ---------------------------------------------------------------------------
# 3 · SILVER / GOLD
# ---------------------------------------------------------------------------
y = ing_bottom + 70
c.label(70, y, "3 · SILVER e GOLD — modelagem, qualidade, layout", 24, VIOLET)

s = c.stacker(COL, y + 50)
s.put(
    0,
    W,
    "SCD Type 2 via AUTO CDC",
    "AUTO CDC ... STORED AS SCD TYPE 2 sobre produto e domicílio. Escrever MERGE à mão daria o "
    "mesmo resultado com mais superfície de erro (ADR-0008).",
    BLUE,
    BG_BLUE,
)
s.put(
    0,
    W,
    "Qualidade em duas camadas",
    "DQX + expectations governadas em UC. Linha reprovada vai para quarentena com rule_name, "
    "rule_expression e failed_at; QLT-003 exige input = passed + quarantined, sem tolerância.",
    BLUE,
    BG_BLUE,
)
s.put(
    1,
    W,
    "A armadilha do SCD2, medida",
    "Join fato × dimensão pela chave natural, sem predicado de janela de validade, faz fan-out "
    "silencioso: +1,706% de receita. O número fica plausível e errado. MOD-003 assere "
    "cardinalidade 1:1 para que não volte.",
    RED,
    BG_RED,
)
s.put(
    1,
    W,
    "O profiler do DQX é bom demais para confiar",
    "As regras auto-geradas teriam quarentenado 23,6% da receita — codificam como lei a anomalia "
    "presente no momento do profiling. QLT-005: profiling propõe, humano dispõe.",
    RED,
    BG_RED,
)
s.put(
    2,
    W,
    "Layout: liquid clustering, sem partition",
    "cluster_by_auto + Predictive Optimization. Sem partitioning e sem Z-ORDER (ADR-0007). "
    "PRF-006 assere que os conjuntos 'sob PO' e 'sob OPTIMIZE agendado' são disjuntos — rodar os "
    "dois na mesma tabela é anti-pattern documentado.",
    GREEN,
    BG_GREEN,
)
s.put(
    2,
    W,
    "Reconciliação gold × silver",
    "198.013 linhas / 613.396,36 — variância zero. Conferida a cada execução, não uma vez.",
    GREEN,
    BG_GREEN,
)
s.put(
    3,
    W,
    "UC Metric Views (MOD-005)",
    "CREATE VIEW ... WITH METRICS LANGUAGE YAML. Registra table_type = METRIC_VIEW. Selecionar a "
    "medida sem MEASURE() é RECUSADO — é isso que separa métrica governada de view bem nomeada. "
    "Derivadas compõem: MEASURE(a)/MEASURE(b), nunca SQL repetido.",
    GREEN,
    BG_GREEN,
)
s.put(
    3,
    W,
    "UC Domains (GOV-003)",
    "Não existe objeto 'domain' isolado: é tag policy governada + registro. Valor fora da lista é "
    "recusado com INVALID_PARAMETER_VALUE. Chave SEM policy aceita qualquer coisa — aí a etiqueta "
    "não governa nada.",
    GREEN,
    BG_GREEN,
)
sg_bottom = s.bottom()
c.frame(52, y + 36, 2160, sg_bottom - (y + 36) + 20)

# ---------------------------------------------------------------------------
# 4 · LAB DE PERFORMANCE
# ---------------------------------------------------------------------------
y = sg_bottom + 70
c.label(70, y, "4 · LAB DE PERFORMANCE — skew, spill e o que não dá para medir aqui", 24, VIOLET)
c.note(
    70,
    y + 34,
    1400,
    "Base: 2X-Small serverless + Photon · transactions 2.595.732 · causal 36.786.524 · "
    "84 execuções registradas, cada uma com seu statement_id.",
    GREY,
)

s = c.stacker(COL, y + 84)
s.put(
    0,
    W,
    "O skew existe, e é enorme",
    "max/median por chave:\nSTORE_ID    2.519×\nPRODUCT_ID  9.926×\nO teste exige >1.000× e >5.000× "
    "para que a premissa do lab falhe se o dado mudar.",
    ORANGE,
    BG_YELLOW,
)
s.put(
    0,
    W,
    "…e a AQE não encosta nele",
    "O skew join da AQE exige AS DUAS condições: fator ≥5× E partição ≥256 MB. STORE_ID passa o "
    "fator por três ordens de grandeza e a partição mais quente fica uma ordem ABAIXO do piso de "
    "bytes. Resultado: AQE, corretamente, não faz nada.",
    RED,
    BG_RED,
)
s.put(
    1,
    W,
    "Resultado negativo mantido",
    "A chave de join real é composta: (PRODUCT_ID, STORE_ID, WEEK_NO). Em 1024 partições ela dá "
    "max/median < 1,5 e não dispara a condição de fator. Ou seja: salting seria injustificado "
    "neste dataset. O teste existe para impedir alguém de reintroduzir salt 'porque tem skew'.",
    GREEN,
    BG_GREEN,
)
s.put(
    1,
    W,
    "",
    "spark.sql.adaptive.enabled\n"
    "spark.sql.adaptive.skewJoin.enabled\n"
    "spark.sql.shuffle.partitions\n"
    "→ CONFIG_NOT_AVAILABLE · SQLSTATE 42K0I",
    GREY,
    BG_GREY,
    13,
    mono=True,
)
s.put(
    2,
    W,
    "Sem Spark UI no serverless",
    "Nenhuma métrica por task existe. Consequência afiada: o limiar de skew que a própria "
    "Databricks documenta — max task > 1,5× o p75 — é IMENSURÁVEL na plataforma que o recomenda. "
    "Isso é limitação de serverless, não de Free Edition: quem padroniza em serverless herda "
    "independentemente do que paga.",
    RED,
    BG_RED,
)
s.put(
    2,
    W,
    "A evidência vem de onde dá",
    "system.query.history + Query Profile. shuffle_read_bytes existe e é sempre 0; "
    "spilled_local_bytes popula. Daí a largura de UnsafeRow ser estimada — e um teste falha no dia "
    "em que a coluna passar a valer, para trocar estimativa por medição.",
    ORANGE,
    BG_YELLOW,
)
s.put(
    3,
    W,
    "O instrumento quase inventou 84 resultados",
    "O lab invalidava o cache variando um COMENTÁRIO na query. O Databricks tira comentário da "
    "chave de cache — as 84 execuções teriam sido internamente consistentes e completamente sem "
    "sentido. Só apareceu porque read_bytes voltava 0. Hoje: literal variável na projeção externa, "
    "e um teste rejeita run com from_result_cache.",
    RED,
    BG_RED,
)
s.put(
    3,
    W,
    "Spill acompanha largura, não linhas",
    "O que derrama é a largura da chave de ordenação, não a contagem de linhas — e em serverless "
    "não existe 'adicionar memória', que é a primeira recomendação da Databricks para spill. Toda "
    "intervenção vira mudança de código.",
    ORANGE,
    BG_YELLOW,
)
perf_bottom = s.bottom()
c.frame(52, y + 70, 2160, perf_bottom - (y + 70) + 20)

# ---------------------------------------------------------------------------
# 5 · ML e AGENTE
# ---------------------------------------------------------------------------
y = perf_bottom + 70
c.label(70, y, "5 · ML e CAMADA AGÊNTICA — os dois portões", 24, VIOLET)

s = c.stacker(COL, y + 50)
s.put(
    0,
    W,
    "ML: LightGBM + MLflow 3 + UC Registry",
    "Janela de lapso pré-registrada ANTES de existir modelo: dia 547 (23 semanas / 161 dias), por "
    "raciocínio de negócio. O mesmo número alimenta o KPI lapsed_households — KPI e modelo não "
    "podem discordar do que é 'sumido'.",
    ORANGE,
    BG_YELLOW,
)
s.put(
    0,
    W,
    "O portão REPROVOU o modelo",
    "PR-AUC 0,1420 contra baseline de recência 0,3846 → −63,1% frente aos +10% exigidos. "
    "Não registrado, sem alias champion. MLR-004 impede a promoção.",
    RED,
    BG_RED,
)
s.put(
    1,
    W,
    "E por que não foi 'consertado'",
    "Mover a janela para o dia 660 levaria a taxa base de 3,0% para 12,4% e tornaria o problema "
    "aprendível. Isso é escolher o experimento pelo resultado desejado — não foi feito e está "
    "registrado como não feito.\n\n"
    "Achado mais duro: a avaliação era subdimensionada desde o início — 76 positivos, 19 no split "
    "de teste. Não resolveria a diferença em nenhuma direção. E dunnhumby selecionou 'frequent "
    "shoppers', então o critério de inclusão do dataset torna o próprio problema quase "
    "inaprendível.",
    RED,
    BG_RED,
)
a1 = s.put(
    2,
    W,
    "Agente: tools são UC Functions",
    "3 funções registradas em Unity Catalog como ferramentas. O grounding não é instrução de "
    "prompt: se o número não veio de uma tool contra objeto governado, ele não existe (AGT-001). "
    "Endpoint databricks-gpt-oss-120b — tool calling funciona; 11 endpoints disponíveis.",
    BLUE,
    BG_BLUE,
)
s.put(
    2,
    W,
    "Tracing e recusa",
    "MLflow 3 trace por request com inputs, tool calls e saída. AGT-006 exige recusar em vez de "
    "chutar: sem dado de suporte, o agente declara a lacuna. Bloco de reasoning é extraído para "
    "não vazar scratchpad na resposta.",
    BLUE,
    BG_BLUE,
)
s.put(
    3,
    W,
    "Juízes LLM e o gate — 100/100/100",
    "10 casos adversariais (respondível, irrespondível, pegadinha, vazio). Juízes pontuam "
    "groundedness, correctness e relevance; nota abaixo do limiar bloqueia o deploy.",
    GREEN,
    BG_GREEN,
)
s.put(
    3,
    W,
    "A primeira rodada deu 33% no caso FÁCIL",
    "As expectativas estavam escritas como listas exaustivas, então fato correto A MAIS virava "
    "não-conformidade — e o juiz zerou groundedness por uma reclamação de verbosidade. Baixar o "
    "limiar de 90% para 80% seria tuning. Reescrever 'reporte X e Y' como 'DEVE INCLUIR X e Y; "
    "fatos extras da mesma tool são bem-vindos' corrige um enunciado que dizia outra coisa. "
    "Os limiares não foram tocados.",
    RED,
    BG_RED,
)
ml_bottom = s.bottom()
c.frame(52, y + 36, 2160, ml_bottom - (y + 36) + 20)

# ---------------------------------------------------------------------------
# 6 · CI/CD
# ---------------------------------------------------------------------------
y = ml_bottom + 70
c.label(70, y, "6 · AMBIENTES E ENTREGA", 24, VIOLET)

s = c.stacker(COL, y + 50)
s.put(
    0,
    W,
    "",
    "target (databricks.yml)\n"
    "  → var.catalog\n"
    "    → pipeline configuration\n"
    "      → spark.conf.get('dng.catalog')\n"
    "        → código",
    GREY,
    BG_GREY,
    13,
    mono=True,
)
s.put(
    0,
    W,
    "ENV-001 é lint de AST, não de regex",
    "Varre apenas literais que o interpretador AVALIA — docstring e comentário ficam fora por "
    "construção, senão a regra dispara na própria documentação dela e alguém a desliga. Arquivos "
    "shipados vêm dos globs de resources/*.yml, então a lista não desatualiza.",
    VIOLET,
    BG_VIOLET,
)
s.put(
    1,
    W,
    "test e prod em mode: production",
    "Com mode: development o target de teste resolvia para OUTRO recurso: nome prefixado, "
    "development semantics forçado e deployment lock DESLIGADO. O teste passava numa forma que "
    "produção não recebe. Hoje os dois configs diferem apenas por substituição test→prod, "
    "asserido folha a folha.",
    VIOLET,
    BG_VIOLET,
)
s.put(
    1,
    W,
    "Proveniência lida de volta",
    "state/metadata.json no workspace carrega config.bundle.git.commit. Prod só sobe o SHA que o "
    "job de teste emitiu — não uma segunda leitura de github.sha, que só coincide até o primeiro "
    "re-run.",
    VIOLET,
    BG_VIOLET,
)
s.put(
    2,
    W,
    "Rollback: rename é update in-place",
    "Redeploy do commit anterior restaura as definições e o pipeline_id NÃO muda. Se o CLI "
    "recriasse em vez de renomear, a recuperação documentada apagaria o histórico do pipeline a "
    "cada uso. Asserido, não presumido.\n\n"
    "O que ele NÃO cobre: dado. Deploy que rodou e gravou linha errada não é desfeito — as "
    "definições voltam, as linhas ficam.",
    GREEN,
    BG_GREEN,
)
s.put(
    3,
    W,
    "Um job pulado aparece como verde",
    "O job de bundle validate é condicionado a vars.DATABRICKS_HOST, que está vazio. Resultado: "
    "`validate -t prod` falhou por toda a vida do arquivo (pipeline com development: true sob "
    "mode: production) e o CI reportou verde o tempo todo. Mitigação: tudo o que dá para checar "
    "offline virou teste unitário incondicional.",
    RED,
    BG_RED,
)
s.put(
    3,
    W,
    "Rastreabilidade como gate",
    "53 requisitos numerados · 26 PASSING · 1 WAIVED com custo medido · 26 PLANNED. Requisito sem "
    "linha quebra o build; linha PASSING apontando para arquivo inexistente também.",
    GREEN,
    BG_GREEN,
)
cd_bottom = s.bottom()
c.frame(52, y + 36, 2160, cd_bottom - (y + 36) + 20)

# ---------------------------------------------------------------------------
# 7 · LIMITES MEDIDOS
# ---------------------------------------------------------------------------
y = cd_bottom + 70
c.label(70, y, "7 · LIMITES DA PLATAFORMA — medidos, não lidos na documentação", 24, VIOLET)

limits = [
    (
        "Criar catálogo",
        "REST e `databricks catalogs create` falham com 'Metastore storage root URL does not exist' "
        "— exigem storage_root que a conta não tem. DDL SQL via Statement Execution API funciona. A "
        "mensagem aponta para a UI, a única opção que não dá para automatizar.",
    ),
    (
        "1 pipeline por tipo",
        "Bronze, silver e gold compartilham um grafo. Custo honesto: defeito no gold derruba a "
        "ingestão. Em tier pago seriam dois pipelines por domínios de falha independentes.",
    ),
    (
        "libraries.glob.include",
        "Rejeita asterisco simples ('use double asterisk') e a lista não mistura glob com arquivo "
        "explícito. Foi isso que forçou silver/pipeline/** — para excluir silver/lib/, que a suíte "
        "unitária importa e o grafo não pode executar.",
    ),
    (
        "refresh_selection",
        "Resolve contra o schema default do pipeline. ['agg_household_rfm'] falha com "
        "INVALID_REFRESH_SELECTION.TABLE_NOT_FOUND apontando bronze.*; ['gold.agg_household_rfm'] "
        "funciona. É o que torna rerun de uma camada só viável.",
    ),
    (
        "Update travado em INITIALIZING",
        "Cancelar e resubmeter — não repetir. 5× PYTHON_REPL_CREATION_FAILED ('may be transient': "
        "não era) e 35 min parado; do IDLE, a MESMA requisição completou em 52s. Cheque SELECT 1 no "
        "warehouse antes de concluir 'ambiente caiu': isola compute de pipeline do resto.",
    ),
    (
        "DDL gerada não é escapada",
        "create_streaming_table(schema=...), CREATE FUNCTION comment e qualquer COMMENT '...'. "
        "Apóstrofo em prosa vira PARSE_SYNTAX_ERROR. Escape todo literal.",
    ),
    (
        "Comentário em coluna de MV",
        "COMMENT ON COLUMN funciona; ALTER TABLE ... ALTER COLUMN devolve EXPECT_TABLE_NOT_VIEW "
        "(uma MV do Lakeflow é view) e ALTER VIEW ... ALTER COLUMN não é sintaxe válida. Os "
        "comentários sobrevivem ao refresh — verificado.",
    ),
    (
        "Upload do seed",
        "databricks fs cp é >200× mais lento que files.upload do SDK para o mesmo arquivo na mesma "
        "sessão. Parquet é ganho separado e aditivo: 15,6× em storage. Um contra-resultado mantido: "
        "campaign_desc (30 linhas) ficou MAIOR em Parquet, 0,3×.",
    ),
    (
        "Cota é da conta inteira",
        "Estourar derruba TODO o compute pelo resto do dia — dev, teste e prod juntos. Por isso "
        "var.sample_pct = 0,05 em dev e ENV-004 dispensado. df.cache() levanta erro no serverless.",
    ),
]
lim = c.stacker([70, 800, 1530], y + 50)
for i, (title, body) in enumerate(limits):
    lim.put(i % 3, 680, title, body, ORANGE, BG_YELLOW, 14)
lim_bottom = lim.bottom()
c.frame(52, y + 36, 2160, lim_bottom - (y + 36) + 20)

c.note(
    70,
    lim_bottom + 34,
    2100,
    "Cada número acima veio de uma execução registrada. Seis vezes, neste projeto, o instrumento "
    "de medição falhou de um jeito indistinguível de um resultado real — limiar abaixo do piso de "
    "ruído, grandeza errada descrita com a palavra certa, cache que não invalidava, eval que media "
    "outra coisa, teste errado sobre o formato do dado e harness que chamava de falha o "
    "comportamento correto. Nenhuma foi pega por code review nem pela suíte. Todas por rodar e ler "
    "a saída.",
    GREY,
    16,
)

problems = c.audit()
if problems:
    for p in problems:
        print("AUDIT:", p)
c.save(OUT)
print(f"{OUT.name}: {len(c.elements)} elementos · {len(problems)} problema(s) de layout")
