# A Ponte: agente A2A com MCP por dentro

Implementação de referência em Python 3 para o desafio da Hill Valley Tech. O servidor MCP expõe três tools e um resource em Streamable HTTP; o agente mantém Tasks A2A e usa HTTP MCP real para descobrir tools, ler a política e chamar o servidor.

## Como rodar

Clone limpo, instale as dependências fixadas e gere/exporte uma chave antes de subir o servidor:

```bash
python3 -m pip install --user -e .
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

Em dois terminais separados, a partir da raiz do repositório:

```bash
# Terminal 1
./subir-mcp.sh

# Terminal 2
./subir-agente.sh
```

O servidor MCP fica em `http://localhost:7301/mcp` e o agente em `http://localhost:7300/a2a`. O Agent Card fica em `http://localhost:7300/.well-known/agent-card.json`.

Com ambos ativos, rode o validador:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Para testar a sobrevivência do `requestState` a um reinício, mantenha o mesmo valor de `REQUEST_STATE_SECRET` no terminal ao interromper e iniciar novamente `./subir-mcp.sh`. Gere a chave com `python3 -c "import secrets; print(secrets.token_hex(32))"`; não use um valor publicado ou versionado.

## Onde a ponte acontece

A conversão de `input_required` para `TASK_STATE_INPUT_REQUIRED` ocorre em `agente/agent.py`, no ramo que lê `resultType == "input_required"`, guarda a resposta opaca de `requestState` no objeto da Task e publica a mensagem `alternativas: ...`. A retomada acontece no mesmo arquivo, no tratamento de um `SendMessage` com `taskId`: a escolha é validada contra as alternativas da Task e o agente chama novamente `tools/call` com um id novo, `inputResponses`, a chave original e o `requestState` sem interpretá-lo.

No servidor, `servidor-mcp/server.py` sela o pedido e as alternativas em um estado protegido, devolve o estado na elicitação e reconstrói o pedido original no retry. Assim o estado da reserva pertence ao pedido MCP selado e o estado da conversa pertence à Task A2A.

## Decisões técnicas

- `requestState`: HMAC SHA-256 sobre um envelope `v1.<payload-base64-url>.<assinatura-base64-url>`, com chave obrigatória em `REQUEST_STATE_SECRET`. Qualquer adulteração ou chave incorreta é rejeitada com `-32602`. O envelope expira em 900 segundos (15 minutos), dentro da janela exigida. O payload contém o pedido original e as alternativas; o servidor não mantém estado MRTR entre requests.
- Tasks: mantidas em memória no processo do agente, isoladas por `task.id`. Cada Task guarda separadamente seus dados internos de retomada; as respostas públicas removem esses dados, evitando vazamento do `requestState`.
- Reservas: mantidas em memória no servidor e reiniciadas a partir de `dados/reservas.json` a cada execução do processo MCP. A chave do requestState deve permanecer a mesma em reinícios para que um retry continue válido.
- Transporte: JSON-RPC 2.0 em HTTP, metadados por request, capability elicitation declarada em `_meta`, headers MCP espelhados no corpo e `traceparent` propagado mantendo o trace-id.

## Saída do validador

Última execução local completa:

```text
trace-id desta execucao: 35203bc768b952d11b85056b5b8ac2e4
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
