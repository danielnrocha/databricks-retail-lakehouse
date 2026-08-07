# Plano de sessão — Databricks avançado e engenharia com agentes

**Público:** um engenheiro de dados sênior tocando migração Synapse → Databricks num varejista
grande, hoje usando só o Databricks Assistant.
**Duração:** 90 minutos, com quebra natural aos 45.
**Formato:** repositório aberto na tela, código rodando de verdade, nada de slide.

---

## O princípio que organiza a sessão

A tentação óbvia é mostrar tudo que o projeto faz. Isso seria uma demo, e demo produz admiração
sem transferência — a pessoa sai impressionada e incapaz de reproduzir nada.

Então: **cada bloco começa por um erro, não por uma feature.** O erro é reproduzível, custa
dinheiro real, e passa por code review. A feature aparece depois, como a coisa que existe porque
o erro existe.

Isso também resolve o problema de nível: um sênior não aprende com "olha o que dá pra fazer", mas
para quando vê um bug que ele próprio poderia ter deixado passar.

---

## Bloco 0 — O contexto, em 5 minutos

Não pule e não alongue. O objetivo é só estabelecer que os números da sessão vêm de dados reais.

- **dunnhumby *The Complete Journey*** — dataset aberto (CC BY 4.0) da empresa de ciência de dados
  do varejo alimentar. 2,6M linhas de transação, 2.500 domicílios, 8 tabelas relacionais, mais
  36,8M linhas de exposição promocional.
- Free Edition. Um workspace, um metastore, warehouse 2X-Small. **Todas as restrições estão
  documentadas em `production-delta.md`** — e mostrar isso logo compra credibilidade para tudo que
  vem depois.

Frase que vale dizer em voz alta: *"tudo que Free Edition não faz está escrito. Fingir que é
produção seria o jeito mais rápido de perder você."*

---

## Bloco 1 — O bug de SCD Type 2 que sobrevive a code review (20 min)

**Abre com a pergunta, não com a resposta:**

> "Você tem uma dimensão SCD2 de produto. 92.353 produtos, e só 2% deles mudaram de categoria
> alguma vez. Você faz `JOIN dim_product ON f.product_id = d.product_id`. Quanto isso erra?"

A intuição de quase todo mundo é "quase nada, 2%". Mostre:

```
join só pela chave            201.476 linhas   £623.863,52
join com janela de validade   198.013 linhas   £613.396,36   ← correto
```

**3.463 linhas fantasma. Receita inflada em 1,706%.**

Pontos a extrair, nessa ordem:

1. **O SQL está sintaticamente perfeito.** Não há erro de digitação, não há warning, o job passa
   verde. O erro está no *modelo de tempo*, não no código — e é por isso que review não pega.
2. **O predicado é meio-aberto:** `>= __START_AT AND (__END_AT IS NULL OR < __END_AT)`. Com `<=` no
   limite superior, todo fato que cai exatamente na fronteira de versão é contado duas vezes. Raro
   o bastante pra passar no teste, frequente o bastante pra estar errado.
3. **`AUTO CDC` não gera `is_current`.** Gera `__START_AT` / `__END_AT`. Um ADR deste repositório
   dizia para clusterizar por `(product_id, is_current)` — coluna que não existe. Foi escrito antes
   do código, e a correção está registrada no próprio ADR.

**A defesa:** `gold_reconciliation` é uma *tabela*, não um check único. Roda em todo update. Uma
mudança futura que reintroduza o fan-out vira `reconciles = false` em vez de inflar um dashboard
em silêncio.

> **Ponte para o dia a dia dele:** "quando o arquiteto te perguntar em qual granularidade a CTE
> está, essa é a classe de resposta que ele quer ouvir — não o que a linha faz, mas o que ela
> assume sobre o tempo."

---

## Bloco 2 — Por que o AQE ignorou um skew de 1.511× (20 min)

**Abre com o dado, deixe ele reagir:**

```
582 lojas.  Top 10% carregam 69,3% das linhas.  max/mediana = 2.519×
PRODUCT_ID é pior: 9.926×
```

> "Esse skew é real, não sintético. O que o AQE faz com ele?"

Resposta medida: **nada. Em nenhuma chave, em nenhuma contagem de partição.**

| tabela | chave | N | max/mediana | maior partição | AQE age? |
|---|---|---:|---:|---:|:-:|
| transactions | `STORE_ID` | 1024 | **1.511×** | 2,9 MiB | não |
| transactions | composta | 1024 | 1,08× | 107 KiB | não |

O AQE exige **duas** condições: `> 5× a mediana` **E** `> 256 MB`. A segunda nunca é satisfeita.

**E a razão mais profunda, que só aparece no plano:** `EXPLAIN FORMATTED` mostra
`PhotonBroadcastHashJoin`. `causal` **nunca é shuffled** na chave de join. AQE skew handling é
inaplicável *por construção*, não meramente abaixo do limiar.

**Duas hipóteses que caíram, e valem mais que as que confirmaram:**

- Chave composta herda o skew de `PRODUCT_ID`? **Não.** 9.926× vira **1,08%** no nível de partição.
  Saltear mesmo assim custou **8,9×** — 18.916ms contra 2.125ms. *Salting é a resposta reflexa e
  aqui teria sido um desastre medido.*
- Hint de broadcast ajuda? **Não.** Plano byte-idêntico.

**O achado de plataforma:** nenhuma das seis confs "setáveis" em serverless funciona num SQL
warehouse serverless. Todas retornam `CONFIG_NOT_AVAILABLE`, SQLSTATE `42K0I`. A lista documentada
vale pra compute de *notebook*. A doc não faz a distinção.

E: **a Spark UI não existe em serverless.** Isso não é limitação de Free Edition — quem padroniza
serverless herda o buraco pagando o que for. O limiar oficial da Databricks pra skew (max task >
1,5× p75) é medível **só** na Spark UI. A própria Databricks recomenda uma medição que a própria
plataforma não expõe.

> **Ponte:** "quando te pedirem otimização, a primeira pergunta não é 'como conserto' — é 'o que
> eu consigo medir aqui, e o que a doc assume que eu meço mas eu não consigo?'"

---

## Bloco 3 — Spill é largura, não volume (10 min)

Rápido, porque o dado fala sozinho. Mesmas 36.786.524 linhas, só a largura da chave de ordenação
muda:

| largura da chave | spill |
|---:|---:|
| 10 B | 0 |
| 138 B | 0 |
| 266 B | **58,2 MiB** |
| 522 B | **91,3 MiB** |

Mitigações contra o baseline de 522 B: **pré-agregar 12,5× mais rápido e zero spill**; chave
estreita 4,0×; filtrar antes 4,6×.

E duas que **pioraram**: `PARTITION BY` ficou 1,08× mais lento com 30% mais spill;
`REPARTITION(1024)` 1,25× mais lento com 9% mais.

> A lição: as duas "otimizações" que pioraram são exatamente as que alguém sugere numa reunião sem
> medir.

---

## PAUSA — 5 min

---

## Bloco 4 — O drift que não apareceu, e por quê (15 min)

**Abre confessando um erro:**

> "Eu construí um gerador que injeta drift de schema em três pontos do stream. Subi 401 arquivos,
> rodei o Auto Loader. `_rescued_data` veio **nulo em todas as 200.000 linhas**. O pipeline estava
> certo. O experimento estava errado. Por quê?"

Deixe ele pensar. A resposta:

**Auto Loader infere schema de uma *amostra do diretório*, não do primeiro arquivo em ordem de
chegada.** Todos os arquivos pós-drift já estavam lá. A forma driftada *era* o schema inicial.
Nada evoluiu, nada foi resgatado.

**A generalização que vale a sessão inteira: backfill não consegue demonstrar evolução de schema.**
Drift é propriedade de *chegada ao longo do tempo*. Se você carrega histórico em massa e declara
sua ingestão "testada contra drift", você não testou nada.

Dois corolários:

- **Schema hint é uma classe de drift que você escolheu não ser avisado.** Eu tinha
  `schemaHints: "quantity STRING"` como alargamento defensivo. Isso desligou silenciosamente a
  detecção que a coluna existia pra demonstrar. *Dê hint no ambíguo, nunca no meramente
  inconveniente.*
- **`--full-refresh-all` não reseta o `schemaLocation`.** Refresh reconstrói as tabelas; o schema
  do Auto Loader tem ciclo de vida separado e sobrevive. Você "reseta tudo" e o comportamento
  antigo persiste.

**O que reproduziu exatamente como previsto — o rename:**

| | linhas |
|---|---:|
| total | 200.000 |
| `trans_time` preenchido | 100.637 |
| `transaction_time` preenchido | 99.363 |
| nenhum dos dois | **0** |

Sem erro. Sem rescue. Sem aviso. **Um dashboard filtrando `trans_time` reporta 50,3% dos dados e
parece saudável fazendo isso.** Mais perigoso que erro de tipo porque nada falha: linha resgatada
aparece numa contagem; null aparece como "sem dados nesse período" — resposta de negócio plausível.

---

## Bloco 5 — Qualidade de dados: o profiler quase custou 23,6% da receita (10 min)

Aplicar os 23 candidatos do profiler do DQX **sem revisão** teria posto em quarentena 17.927 linhas
carregando **£146.099,63 — 23,6% da receita**. O ruleset revisado põe 6.

A causa: DQX perfila **amostra de 1.000 linhas**, e seis de onze limites de faixa já são violados
pela tabela cheia no dia um.

O sinal denunciador vale mostrar na tela:

```
quantity_units <= 3893.8152094863253
```

Dezesseis casas decimais numa coluna de contagem. **Limite ajustado, não regra de negócio.**

E a razão de negócio por trás: `QUANTITY` chega a 89.638 legitimamente — item vendido por peso, em
gramas. `SALES_VALUE` é 0 legitimamente — linha totalmente coberta por cupom.

> **Profiling propõe, humano dispõe.** Aqui a diferença é um quarto da receita.

---

## Bloco 6 — O método de trabalho com agentes (20 min)

Esse é o bloco que ele pediu em julho: *"não consigo deixar agente trabalhando pra mim, e se
conseguir, não sei como faz."*

**Não venda o loop. Mostre onde ele falha.**

### O que TDD sozinho não pega

Este repositório tem o recibo: um requisito ficou **verde** enquanto o gerador produzia **73% de
duplicatas contra 0,5% configurado** — porque o teste só afirmava que *alguma* duplicata aparecia.

> "'A feature dispara' e 'a feature dispara na taxa configurada' são afirmações diferentes.
> Verificação de existência é o teste mais barato de escrever e o mais fácil de confundir com
> cobertura."

Três parâmetros de taxa saíram com a unidade errada, e nas três **o código batia com o nome da
variável**:

| parâmetro | configurado | observado |
|---|---:|---:|
| `duplicates.fraction` | 0,5% | **73%** |
| `beyond_watermark_fraction` | 0,200% | **0,003%** |
| `drift.*_at_event` | dispara em 250k | **nunca disparou** (run de 200k) |

O terceiro é o pior: fez o run parecer **limpo**. Resultado limpo é lido como evidência sobre o
pipeline quando é evidência sobre a config.

### Limiares são derivados, nunca escolhidos

Errei isso duas vezes na mesma tarde. Um teste afirmava distância de variação total `< 0,02` —
número escolhido porque parecia pequeno. Falhou em 0,026, e **o teste estava errado, não o
código**: com 582 lojas e 60 mil sorteios, o piso de ruído multinomial é ~0,024. O limiar ficava
*abaixo do piso*.

A correção óbvia — afrouxar até ficar verde — **converte asserção real em decorativa**. A correção
certa mede o baseline a cada run e afirma contra ele.

### Rode a coisa e olhe a saída

Seis defeitos achados assim, **todos invisíveis a code review e à suíte de testes**. O padrão nos
seis: os números errados eram *plausíveis*. Grandes, monotônicos, bem formatados. Pareciam
resultados.

O melhor exemplo: **a Databricks tira comentários da chave de cache de query.** Um laboratório que
invalidava cache com comentário variável teria sido um benchmark de cache do início ao fim — 84
runs medidos, todos sem significado, todos internamente consistentes. Foi pego por um zero numa
coluna que ninguém estava olhando.

### O que não é delegável

Isso é o fecho da sessão, e é o que responde a pergunta de carreira dele:

1. **Definir o que é "bom", antes, de forma falsificável.** O agente otimiza o que você declarar.
2. **Decidir o que vale medir.** Ninguém perguntou se chaves duplicadas em `causal` inflariam o
   LEFT JOIN até um humano se perguntar.
3. **Notar que um número é suspeito.** 990.065 é exatamente 5× 198.013. Máquina que checa "é
   inteiro positivo?" aprova.
4. **Julgar trade-off sem resposta certa.**
5. **Decidir quando parar.**

> "Você não está sendo substituído na parte de escrever código. Você está sendo promovido pra
> parte de decidir o que é certo — e essa parte não tem atalho."

---

## Fecho — 5 min

Uma coisa só, e deixe no ar:

> "Nada nessa sessão veio de eu ser mais inteligente que o problema. Veio de rodar, olhar a saída,
> e escrever o que estava errado antes de consertar. Os três achados mais fortes do projeto foram
> erros meus."

Aponte para `docs/decision-log.md` e para as reversões deixadas visíveis.

---

## Se sobrar tempo, ou para uma segunda sessão

- Os exercícios de quebra-e-conserta em [`exercises.md`](exercises.md) — é onde a transferência
  realmente acontece.
- `production-delta.md` seção por seção: o que muda com service principals de verdade.
- O gerador: por que reamostrar cesta inteira em vez de gerar comportamento por regra, e por que
  isso desqualifica dado amplificado para *avaliar* modelo.
