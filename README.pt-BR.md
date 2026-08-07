# Lakehouse de varejo — plataforma de dados agêntica no Databricks

> Resumo executivo em português. A documentação técnica completa está em inglês —
> ver [`README.md`](README.md) e o índice de leitura no fim desta página.

Um lakehouse de varejo alimentar construído do jeito que uma plataforma de verdade é construída:
requisitos primeiro, testes que os cobram, decisões registradas com o que foi rejeitado, e modos de
falha induzidos de propósito para que as correções sejam **medidas** em vez de afirmadas.

**A alegação que este repositório faz:** qualquer um mostra um pipeline que funciona. Este mostra um
pipeline que **falha corretamente** — alto, em quarentena, com contexto suficiente para
diagnosticar — e prova toda afirmação de performance com um perfil de query em vez de um adjetivo.

📊 **[Os seis achados, em uma página visual](https://claude.ai/code/artifact/23926add-e082-4dc4-8450-1cbb35e353fa)**

---

## O que está aqui, em números medidos

| | |
|---|---|
| Dados | 2.595.732 linhas de transação · 36.786.524 de exposição promocional · 582 lojas |
| Camadas | bronze → silver → gold, rodando em Lakeflow Spark Declarative Pipelines |
| Ambientes | `dng_dev` · `dng_test` · `dng_prod` como catálogos, injetados pelo bundle |
| Testes | 86 unitários, verdes sem conexão com workspace |
| Requisitos | 53 numerados, 26 provados, 1 dispensado com custo medido, gate de CI que quebra o build se faltar teste |
| Conciliação | gold 198.013 linhas / 613.396,36 — variância **zero** contra silver |

---

## Os seis achados

Nenhum destes erros quebrou um pipeline. Todos passaram por revisão, rodaram sem aviso, e
produziram números **grandes, monotônicos e bem formatados**. Foi exatamente por isso que quase
ninguém os teria pego.

**1 · O join de SCD2 sem janela de validade infla a receita em 1,706%.** Só 2% das chaves de
produto têm segunda versão, e mesmo assim o join pela chave sozinha inventa 3.463 linhas. O SQL
está sintaticamente perfeito — o defeito está no modelo de tempo. E o erro é sempre *para cima*,
então nunca dispara um alerta de queda.

**2 · O AQE se recusou a agir sobre skew de 1.511×.** Exige partição maior que 5× a mediana **e**
maior que 256 MB; a segunda nunca acontece. Mais fundo ainda: o plano é `PhotonBroadcastHashJoin`,
a tabela grande nunca é *shuffled* na chave, então o tratamento de skew é inaplicável por
construção. A chave composta dissolve o skew sozinha (9.926× → 1,08×), e saltear mesmo assim foi
medido em **8,9× mais lento**.

**3 · Spill é largura de chave, não volume de linhas.** Mesmas 36,8M linhas: chave de 10 B não
derrama nada, de 522 B derrama 91,3 MiB. Pré-agregar é 12,5× mais rápido com zero spill — e as duas
"otimizações" que *pioraram* são exatamente as que alguém sugere numa reunião sem medir.

**4 · Um rename apagou metade dos dados sem falhar.** `trans_time` virou `transaction_time` no meio
do stream. Sem erro, sem resgate, sem aviso. Um dashboard filtrando o nome antigo reporta **50,3%
dos dados e parece saudável**. Mais perigoso que erro de tipo porque nada falha.

**5 · O profiler de qualidade quase colocou 23,6% da receita em quarentena.** 23 regras geradas de
uma amostra de 1.000 linhas. O sinal denunciador é o próprio número:
`quantity_units <= 3893,8152094863253` — dezesseis casas decimais numa coluna de contagem. Limite
ajustado a dados, não regra de negócio.

**6 · Um teste verde sobre um gerador com 73% de duplicatas contra 0,5% configurado.** Três
parâmetros de taxa saíram com a unidade errada, e nos três o código batia com o nome da variável. O
teste afirmava `duplicate_events > 0`.

---

## O método

Nenhum dos seis foi encontrado lendo código.

- **Limiares são derivados, nunca escolhidos.** Um teste afirmava distância < 0,02; o piso de ruído
  era 0,024, então nenhum código correto passaria. Afrouxar até ficar verde converte asserção real
  em decorativa.
- **Rode a coisa e olhe a saída.** A Databricks remove comentários da chave de cache de query — um
  laboratório que invalidava cache por comentário teria sido um benchmark de cache do início ao
  fim, com 84 execuções internamente consistentes e sem significado nenhum.
- **Verificação independente.** Um segundo passe mediu o join em vez de raciocinar sobre ele e achou
  uma afirmação minha errada por fator de cinquenta.
- **Reversões ficam visíveis.** Três decisões foram revertidas com o texto original intacto.
  Reescrever para parecer premeditado destrói a informação mais útil do registro.

O raciocínio completo está em [ADR-0001](docs/adr/ADR-0001-spec-driven-agent-loop.md), incluindo o
que **não** é delegável a um agente.

---

## O que isto não prova

As lacunas estão escritas, não escondidas — [`production-delta.md`](docs/architecture/production-delta.md)
tem dez seções. As principais:

- **Isolamento é lógico, não físico** — três catálogos num metastore.
- **Sem service principals** — tudo roda como uma pessoa. Maior lacuna do projeto, de longe.
- **Sem Spark UI** — limitação de *serverless*, não de plano gratuito. O limiar oficial da
  Databricks para skew só é medível ali; a plataforma recomenda uma medição que ela mesma não expõe.
- **Um pipeline para todo o medalhão** — um defeito no gold derrubou a ingestão do bronze.

---

## Material de mentoria

- [`session-plan.md`](docs/mentoring/session-plan.md) — roteiro de 90 minutos. Cada bloco abre com
  um erro, nunca com uma feature: demo produz admiração sem transferência.
- [`exercises.md`](docs/mentoring/exercises.md) — seis exercícios de quebre-diagnostique-conserte.
  Dois deles não são sobre consertar nada: um é sobre **resistir** a uma otimização, outro é sobre
  descobrir que o teste estava errado e não o código.

---

## Ordem de leitura

1. [`docs/00-north-star.md`](docs/00-north-star.md) — as cinco decisões de negócio que a plataforma serve
2. [`docs/adr/`](docs/adr/) — cada decisão contestada, com as alternativas rejeitadas e o custo de reversão
3. [`specs/REQUIREMENTS.md`](specs/REQUIREMENTS.md) — 53 requisitos testáveis
4. [`specs/traceability.md`](specs/traceability.md) — o mapa requisito → teste, cobrado no CI
5. [`docs/architecture/`](docs/architecture/) — os achados, com as medições

## Licença

Código: Apache-2.0. Dados: dunnhumby *The Complete Journey*, CC BY 4.0.
