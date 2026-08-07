# Exercícios — quebre, diagnostique, conserte

O plano de sessão transfere conhecimento. Estes exercícios transferem **capacidade**, que é outra
coisa: só se aprende a diagnosticar diagnosticando, e a diferença aparece na primeira vez que o
pipeline quebra às 3h da manhã.

## Como usar

Cada exercício tem a mesma forma:

1. **Quebre** — um comando que introduz o defeito de propósito.
2. **Observe** — o que a plataforma mostra, que quase nunca é o problema.
3. **Diagnostique** — a pergunta a responder *antes* de tocar em qualquer código.
4. **Conserte** — e prove que consertou.

**Regra: não leia a seção "Resposta" antes de escrever a sua.** Ler a resposta produz a sensação de
entendimento sem o entendimento, e a diferença só aparece quando não há resposta escrita.

Trabalhe em `dng_dev`. Se destruir algo, `make bootstrap` recria a estrutura e
`python -m generator --events 200000 --env dev` regenera os dados.

---

## E1 — O join que infla a receita

**Nível:** fundamental. Se você só fizer um, faça este.

### Quebre

Em `src/retail_lakehouse/gold/pipeline/fct_basket_line.py`, remova as duas linhas do predicado de
validade dentro de `as_of`, deixando só a igualdade de chave:

```python
def as_of(dim: DataFrame, key: str) -> DataFrame:
    return dim.alias("d").join(
        fact.alias("f"),
        (F.col(f"d.{key}") == F.col(f"f.{key}")),   # janela removida
        "inner",
    )
```

Deploy e run.

### Observe

O pipeline passa. Todos os flows ficam verdes. Nenhum aviso.

### Diagnostique

Antes de olhar qualquer tabela, responda no papel:

1. A contagem de linhas da fato vai subir, descer ou ficar igual? **Por quê?**
2. Qual é o fator de multiplicação esperado, em termos das versões da dimensão?
3. Qual métrica de negócio vai estar errada, e em que direção?
4. Um teste que só verifica `count(*) > 0` pegaria isso? E um que compara com a corrida anterior?

Só depois:

```sql
SELECT * FROM dng_dev.gold.gold_reconciliation;
```

### Conserte e prove

Restaure o predicado. A prova não é "voltou ao normal" — é `reconciles = true` com
`row_variance = 0` e `revenue_variance = 0.00`.

### Resposta

<details>
<summary>Abra só depois de escrever a sua</summary>

Sobe. Cada fato casa com **todas** as versões da sua dimensão, não com a vigente no momento do
fato. Como 1.837 produtos têm 2 versões, esses fatos duplicam: **+3.463 linhas, +1,706% de
receita**.

A direção importa: o erro é sempre **para cima**, nunca para baixo. Isso significa que ele nunca
dispara um alerta de "receita caiu" — só faz todo mundo comemorar um número que não existe.

`count(*) > 0` não pega. Comparar com a corrida anterior **pega**, e é por isso que
`gold_reconciliation` existe como tabela e não como consulta ad hoc.
</details>

---

## E2 — O drift que some

**Nível:** intermediário. Ensina uma classe de erro experimental, não só um bug.

### Quebre

Nada a quebrar — o defeito já está lá, e o exercício é notar.

```sql
SELECT count(*) AS total,
       sum(CASE WHEN _rescued_data IS NOT NULL THEN 1 ELSE 0 END) AS rescued
FROM dng_dev.bronze.basket_line_events_raw;
```

Você verá 200.000 e **0**. Mas o gerador injetou três eventos de drift de propósito, e o manifesto
prova:

```bash
grep -E 'drift|late|duplicate' data/generated/stress/_manifest.json
```

### Diagnostique

1. O Auto Loader infere schema de onde, exatamente? Do primeiro arquivo? De todos? De uma amostra?
2. Em que ordem os 401 arquivos foram disponibilizados em relação à primeira execução?
3. O que `--full-refresh-all` reconstrói, e o que ele **não** toca?
4. Desenhe a ordem de operações que *faria* o rescue acontecer.

### Conserte

Reproduza o drift de verdade. Ingestão estagiada:

```bash
# 1. Limpe o volume e o schema location
# 2. Suba SÓ os arquivos anteriores ao primeiro ponto de drift
# 3. Rode o pipeline  -> schema v0 estabelecido
# 4. Suba a próxima tranche (pós-drift)
# 5. Rode de novo    -> agora sim
```

Prova: `_rescued_data` não-nulo, e mais de uma versão em
`/Volumes/dng_dev/bronze/landing/_schema/_schemas/`.

### Resposta

<details>
<summary>Depois de tentar</summary>

De uma **amostra do diretório**, não do primeiro arquivo em ordem de chegada. Como todos os 401
arquivos já estavam lá, a forma pós-drift *era* a forma inicial. Nada evoluiu porque nada mudou
depois da inferência.

`--full-refresh-all` reconstrói as tabelas. O `cloudFiles.schemaLocation` é artefato separado com
ciclo de vida próprio e **sobrevive** — por isso você "reseta tudo" e o comportamento persiste.

**A lição que passa deste exercício para o seu trabalho: backfill não consegue demonstrar evolução
de schema.** Se a sua migração carrega histórico em massa e você declara a ingestão testada contra
drift, você não testou nada.
</details>

---

## E3 — Regra de qualidade que come receita

**Nível:** intermediário. Ensina a duvidar de ferramenta boa.

### Quebre

Em `src/retail_lakehouse/quality/rules.py`, adicione a regra que o profiler do DQX gerou:

```python
# candidato gerado por profiling — NÃO revisado
{"name": "quantity_units_in_range", "criticality": "error",
 "check": {"function": "is_in_range",
           "arguments": {"column": "quantity_units", "min_limit": 0,
                         "max_limit": 3893.8152094863253}}}
```

Deploy e run.

### Diagnostique

1. Antes de olhar: quantas linhas você espera na quarentena? Ordem de grandeza.
2. `3893.8152094863253` — de onde veio esse número? O que significam as 16 casas decimais?
3. `quantity_units` chega a 89.638 no dado real. Isso é erro de dado ou fato de negócio?
4. Se fosse fato de negócio, que informação a regra precisaria ter para estar certa?

```sql
SELECT count(*) AS quarantined, round(sum(sales_amt),2) AS revenue_lost
FROM dng_dev.silver.fact_basket_line_quarantine;
```

### Resposta

<details>
<summary>Depois de estimar</summary>

**17.927 linhas, £146.099,63 — 23,6% da receita.**

O número vem do intervalo interquartil de uma **amostra de 1.000 linhas**. As 16 casas decimais são
a assinatura: é um limite *ajustado a dados*, não uma regra de negócio. Nenhum humano escreveria
3893,8152094863253 como limite de contagem.

89.638 é fato de negócio: item vendido **por peso**, quantidade expressa em gramas. A regra correta
precisa saber a *unidade de venda do produto* — e essa informação está na dimensão, não na fato.

Corolário desconfortável: o profiler estava fazendo exatamente o que promete. A ferramenta não
falhou. Falhou quem aplicou sem revisar.
</details>

---

## E4 — Otimize algo que não precisa

**Nível:** avançado. Ensina a não fazer.

### Quebre

Nada. Este exercício é sobre **resistir** a uma mudança.

Cenário: alguém numa reunião diz *"o `PRODUCT_ID` tem skew de 9.926×, precisa de salting"*.
Você é o engenheiro. O que faz?

### Diagnostique

1. Qual a chave do join que realmente roda? É `PRODUCT_ID` sozinho?
2. Meça o skew **da chave real**, não da coluna isolada.
3. Qual o tamanho da maior partição em bytes? Compare com 256 MB.
4. Olhe o plano: `EXPLAIN FORMATTED`. Que tipo de join é? Há shuffle na chave?
5. Só então: salting ajudaria?

### Resposta

<details>
<summary>Depois de medir</summary>

A chave real é `(PRODUCT_ID, STORE_ID, WEEK_NO)`. O skew de 9.926× do `PRODUCT_ID` isolado vira
**1,08× no nível de partição** — a chave composta dissolve o skew.

O plano é `PhotonBroadcastHashJoin`: `causal` **não é shuffled** na chave. AQE skew handling é
inaplicável por construção.

Saltear mesmo assim, medido: **8,9× mais lento** — 18.916ms contra 2.125ms.

**A lição:** a otimização reflexa teria degradado o job em quase 9× e apareceria como trabalho
entregue. Um resultado negativo medido vale mais que uma otimização não medida — e defender "não
vamos fazer isso, aqui está a medição" é uma habilidade sênior que quase ninguém pratica.
</details>

---

## E5 — Escreva um teste que não pode passar

**Nível:** avançado. Este é sobre você, não sobre o pipeline.

### Quebre

Escreva um teste afirmando que o sampler preserva a distribuição de lojas, com um limiar que você
escolheu porque *parece pequeno*:

```python
assert total_variation_distance < 0.01
```

Rode. Falha.

### Diagnostique

1. O código está errado ou o teste está errado? **Como você distingue?**
2. Com 582 lojas e 60.000 sorteios, qual é a distância de variação total *esperada* de um sampler
   perfeito? (Dica: ruído multinomial.)
3. Se você afrouxar o limiar até passar, o que o teste passa a afirmar?
4. Qual seria o baseline correto, e como você o mede em vez de escolher?

### Resposta

<details>
<summary>Depois de pensar</summary>

O teste. O piso de ruído é ~0,024, então **nenhum sampler, por mais correto, passaria** em 0,01 ou
em 0,02. O limiar estava abaixo do achievable.

Afrouxar até ficar verde converte uma asserção real numa decorativa: o teste passa a afirmar
"algum número é menor que outro número que escolhi para ser maior que ele".

O baseline certo é **medido**: sorteie da distribuição verdadeira, calcule a distância que um
sampler ideal produz *naquela execução*, e afirme que o real está dentro de um múltiplo disso.

Cometi esse erro duas vezes na mesma tarde — a segunda com um limiar de cobertura de 90% que
falhou em 73%, onde 73% estava correto (estatística do coletor de cupons numa cauda de 582 lojas).

**Se você não consegue derivar um limiar, você não entendeu o que está afirmando.**
</details>

---

## E6 — Faça um agente errar em silêncio

**Nível:** avançado. É o exercício mais próximo do trabalho real de 2026.

### Quebre

Peça a um agente: *"gere dados de teste com 0,5% de duplicatas"*. Não especifique mais nada.

### Diagnostique

Antes de aceitar o resultado:

1. 0,5% **de quê**? Das linhas de saída? Por lote? Por chamada? Quantas leituras plausíveis existem?
2. Como você verifica a taxa **observada** sem confiar no relato do agente?
3. Que teste o agente provavelmente escreveu? Ele distingue "a feature existe" de "a feature está
   correta"?
4. Se a taxa saísse 73% em vez de 0,5%, o que na sua verificação pegaria?

### Conserte

Escreva o teste que compara **configurado contra observado** para *toda* taxa, num lugar só. Não um
teste por cenário — um teste que varre todos.

Referência: `tests/unit/test_generator_emit.py::test_configured_rates_match_observed_rates`.

### Resposta

<details>
<summary>Depois de tentar</summary>

Aconteceu exatamente isso neste repositório. `fraction` foi implementado como *probabilidade, por
cesta, de replayar a janela inteira*. Com janela de 5.000 eventos e ~22.000 cestas: **73% de
duplicatas contra 0,5% configurado.**

O teste existia e estava verde. Afirmava `duplicate_events > 0`.

E cascateou: a enxurrada distorceu a distribuição de lojas de 582 para 400 e o top-decil de 67% para
53%. Um experimento de skew rodado naquele stream teria medido o bug do gerador e atribuído ao
varejo alimentar.

O mesmo defeito apareceu **três vezes**, em três parâmetros, e nas três o código batia com o nome
da variável. Só as unidades estavam erradas.

**Parâmetro de taxa merece declarar sua unidade explicitamente, e merece um teste que compara
configurado contra observado.** "A feature dispara" e "a feature dispara na taxa configurada" são
afirmações diferentes.
</details>

---

## Rubrica

Você absorveu isto quando consegue, **sem consultar**:

- [ ] Explicar por que um join SCD2 sem janela infla e sempre para cima
- [ ] Dizer o que medir para provar skew numa plataforma sem Spark UI
- [ ] Explicar por que backfill não testa evolução de schema
- [ ] Argumentar contra uma otimização usando medição, não intuição
- [ ] Derivar um limiar de teste em vez de escolher
- [ ] Nomear três coisas que um agente **não** pode decidir por você

O último é o que separa quem usa IA de quem é responsável pelo resultado dela.
