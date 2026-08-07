# Diagramas

Dois diagramas, dois públicos. Nenhum dos dois substitui o outro: o primeiro não tem detalhe
suficiente para trabalhar, e o segundo é ilegível para quem não é da área.

| Arquivo | Para quem | O que responde |
|---|---|---|
| `arquitetura-plataforma.excalidraw` | negócio, liderança, onboarding | Por que a plataforma existe, o que ela decide, o que custou |
| `referencia-tecnica.excalidraw` | engenheiro de dados Databricks | O que foi usado, o que foi medido, onde a plataforma não entrega o que a documentação promete |

Abra em [excalidraw.com](https://excalidraw.com) (*Menu → Open*) ou na extensão do VS Code.

## São artefatos gerados, não desenhados

O texto e a diagramação vivem em `scripts/`; o `.excalidraw` é a saída.

```bash
python3 scripts/make_architecture_diagram.py   # visão de negócio
python3 scripts/make_technical_diagram.py      # referência técnica
```

Editar o `.json` à mão funciona uma vez e é sobrescrito na próxima execução. Se você fizer isso,
traga a mudança de volta para o script.

O motivo é o mesmo que levou `scripts/publish_governance.py` a existir: um artefato que só existe
porque alguém o montou uma vez não pode ser refeito, revisado em diff, nem reproduzido por outra
pessoa. Ver a entrada de 2026-08-07 em `docs/decision-log.md` sobre a política de tags que existia
apenas no workspace.

## `scripts/excalidraw_kit.py`

Compartilhado pelos dois geradores. Três decisões que valem saber antes de mexer:

**A altura da caixa sai do texto já quebrado**, com 8px de margem — nada de altura fixa com sobra.
A largura de caractere é estimada, e ela erra de propósito **para baixo**: se subdimensiona, o
Excalidraw quebra em mais linhas ao abrir e cresce o container, e o encaixe continua justo. Errar
para cima deixaria buraco que ninguém corrige depois. A única exceção é o `chip()`, que
superdimensiona 18% porque um chip que quebra em duas linhas destrói o alinhamento da fileira.

**`Stack` empilha por coluna a partir da altura real do card anterior.** A primeira versão usava
offsets fixos (`y + 200`, `y + 230`). Funcionavam com a estimativa daquele dia e colidiriam no
instante em que o Excalidraw crescesse uma caixa — que é justamente o comportamento em que o
dimensionamento se apoia.

**`Canvas.audit()` reprova** colisão de caixa, texto estourando o container, ligação órfã e seta
cruzando título. Rodar isso é metade do motivo de gerar em vez de desenhar: revisão a olho não pega
uma seta cruzando um título duas seções abaixo — e não pegou, na primeira versão.

Para conferir que o layout aguenta a estimativa de fonte errar, rode os geradores com
`Canvas.char_width` inflado. Ambos passam limpos até 1,7×.

## Estilo

Nativo do Excalidraw, sem CSS nem tema custom: **Excalifont** (`fontFamily: 5`) na prosa,
**Cascadia** (`fontFamily: 3`) apenas onde o conteúdo É código — nome de opção, SQLSTATE,
assinatura de API. Traço desenhado à mão (`roughness: 1`), paleta e setas padrão. Posição, ângulo e
semente de cada elemento recebem ruído pequeno e **determinístico**: o desenho não deve parecer
saída de gerador, mas rodar duas vezes tem de produzir o mesmo arquivo, senão todo commit vira um
diff de ruído.
