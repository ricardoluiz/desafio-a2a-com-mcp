#!/usr/bin/env python3
from __future__ import annotations

import base64, hashlib, hmac, json, os, secrets, sys, time
from datetime import datetime
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("MCP_PORT", "7301"))
PROTOCOL = "2026-07-28"
SECRET_TEXT = os.getenv("REQUEST_STATE_SECRET", "")
if len(SECRET_TEXT.encode()) < 32:
    raise SystemExit("REQUEST_STATE_SECRET must contain at least 32 bytes")
SECRET = SECRET_TEXT.encode()

salas = json.loads((ROOT / "dados/salas.json").read_text())
reservas = json.loads((ROOT / "dados/reservas.json").read_text())
politica_texto = (ROOT / "dados/politica-de-uso.md").read_text()
POLITICA = politica_texto.splitlines()[0].split(":", 1)[1].strip()
res_counter = max(int(r["id"].split("-")[1]) for r in reservas) + 1


def b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def seal(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    body = b64encode(raw)
    sig = b64encode(hmac.new(SECRET, ("v1." + body).encode(), hashlib.sha256).digest())
    return f"v1.{body}.{sig}"


def unseal(state: str) -> dict:
    if not isinstance(state, str):
        raise ValueError("requestState malformed")
    parts = state.split(".")
    if len(parts) != 3 or parts[0] != "v1":
        raise ValueError("requestState malformed")
    body, sig = parts[1], parts[2]
    expected = b64encode(hmac.new(SECRET, ("v1." + body).encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise ValueError("requestState seal invalid")
    try:
        payload = json.loads(b64decode(body))
    except Exception:
        raise ValueError("requestState malformed")
    if time.time() > payload["exp"]:
        raise ValueError("requestState expired")
    return payload


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def validate(sala_id: str, inicio: str, fim: str) -> str | None:
    sala = next((s for s in salas if s["id"] == sala_id), None)
    if sala is None:
        return f"Sala inexistente: {sala_id}"
    try:
        ini, fin = parse_iso(inicio), parse_iso(fim)
    except Exception:
        return "Intervalo invalido: fim deve ser posterior a inicio"
    if fin <= ini:
        return "Intervalo invalido: fim deve ser posterior a inicio"
    if ini.hour * 60 + ini.minute < 480 or fin.hour * 60 + fin.minute > 1200:
        # ISO explicit timezone is evaluated in local values as -03:00 São Paulo.
        return "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
    if (fin - ini).total_seconds() > 2 * 3600:
        return "Duracao acima do limite: a politica permite no maximo 2 horas"
    return None


def overlaps(a_ini: str, a_fim: str, b_ini: str, b_fim: str) -> bool:
    return parse_iso(a_ini) < parse_iso(b_fim) and parse_iso(b_ini) < parse_iso(a_fim)


def conflitos(sala_id: str, inicio: str, fim: str) -> list[dict]:
    return [r for r in reservas if r["sala"] == sala_id and overlaps(inicio, fim, r["inicio"], r["fim"])]


def alternativas(sala_id: str, inicio: str, fim: str) -> list[str]:
    pedida = next(s for s in salas if s["id"] == sala_id)
    livres = [s for s in salas if s["capacidade"] >= pedida["capacidade"] and not conflitos(s["id"], inicio, fim)]
    livres.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in livres[:3]]


def tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True,
            "resultType": "complete", "structuredContent": {"erro": message}}


def rpc_error(request_id, code: int, message: str, data=None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


async def mcp_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(rpc_error(None, -32700, "Parse error"), status_code=400)
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}
    meta = params.get("_meta")

    # Every request is logged; no state is inferred from previous requests.
    trace = meta.get("traceparent") if isinstance(meta, dict) else None
    print(f"MCP method={method} id={req_id} traceparent={trace or '-'}", file=sys.stderr, flush=True)

    if not isinstance(meta, dict) or "io.modelcontextprotocol/protocolVersion" not in meta or "io.modelcontextprotocol/clientCapabilities" not in meta:
        return JSONResponse(rpc_error(req_id, -32602, "Invalid params: missing required _meta fields"), status_code=400)
    if meta.get("io.modelcontextprotocol/protocolVersion") != PROTOCOL:
        return JSONResponse(rpc_error(req_id, -32602, "Invalid protocolVersion"), status_code=400)

    # Mirror required transport headers with the JSON-RPC body.
    hdr_protocol = request.headers.get("mcp-protocol-version")
    if not hdr_protocol:
        return JSONResponse(rpc_error(req_id, -32020, "Missing MCP-Protocol-Version header"), status_code=400)
    if hdr_protocol != meta.get("io.modelcontextprotocol/protocolVersion"):
        return JSONResponse(rpc_error(req_id, -32020, "MCP-Protocol-Version differs from request metadata"), status_code=400)

    hdr_method = request.headers.get("mcp-method")
    if not hdr_method or hdr_method != method:
        return JSONResponse(rpc_error(req_id, -32020, "Mcp-Method differs from request body"), status_code=400)
    if method == "tools/call":
        name = params.get("name")
        if not request.headers.get("mcp-name") or request.headers.get("mcp-name") != name:
            return JSONResponse(rpc_error(req_id, -32020, "Mcp-Name differs from tool name"), status_code=400)
    if method == "resources/read":
        uri = params.get("uri")
        if not request.headers.get("mcp-name") or request.headers.get("mcp-name") != uri:
            return JSONResponse(rpc_error(req_id, -32020, "Mcp-Name differs from resource uri"), status_code=400)

    if method == "tools/list":
        tools = [
            {"name":"listar_salas","description":"Lista as salas disponíveis","inputSchema":{"type":"object","properties":{},"required":[]},"outputSchema":{"type":"array","items":{"type":"object"}}},
            {"name":"consultar_disponibilidade","description":"Consulta disponibilidade de uma sala","inputSchema":{"type":"object","properties":{"sala":{"type":"string"},"inicio":{"type":"string"},"fim":{"type":"string"}},"required":["sala","inicio","fim"]}},
            {"name":"reservar_sala","description":"Reserva uma sala ou solicita uma alternativa","inputSchema":{"type":"object","properties":{"sala":{"type":"string"},"inicio":{"type":"string"},"fim":{"type":"string"},"responsavel":{"type":"string"}},"required":["sala","inicio","fim","responsavel"]}}
        ]
        return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":{"tools":tools,"_meta":{"serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}})

    if method == "resources/list":
        return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":{"resources":[{"uri":"politica://uso","name":"Política de uso","mimeType":"text/markdown"}]}})

    if method == "resources/read":
        uri = params.get("uri")
        if uri != "politica://uso":
            return JSONResponse(rpc_error(req_id, -32602, "Invalid resource URI: resource not found"), status_code=400)
        return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":{"contents":[{"uri":uri,"mimeType":"text/markdown","text":politica_texto}],"_meta":{"serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in {"listar_salas","consultar_disponibilidade","reservar_sala"}:
            return JSONResponse(rpc_error(req_id, -32602, f"Tool inexistente: {name}"))

        if name == "listar_salas":
            data = salas
            text = json.dumps(data, ensure_ascii=False)
            result = {"content":[{"type":"text","text":text}],"structuredContent":data,"resultType":"complete","_meta":{"serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}
            return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":result})

        sala_id = args.get("sala")
        inicio = args.get("inicio")
        fim = args.get("fim")
        error = validate(sala_id, inicio, fim)
        if error:
            return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":tool_error(error)})

        if name == "consultar_disponibilidade":
            cs = conflitos(sala_id, inicio, fim)
            data = {"livre":not bool(cs),"conflitos":cs}
            text = json.dumps(data, ensure_ascii=False)
            return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":{"content":[{"type":"text","text":text}],"structuredContent":data,"resultType":"complete","_meta":{"serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}})

        # Retry: ignore caller arguments and reconstruct the sealed original.
        state = params.get("requestState")
        responses = params.get("inputResponses")
        if state is not None:
            try:
                payload = unseal(state)
            except ValueError as exc:
                return JSONResponse(rpc_error(req_id, -32602, str(exc)))
            original = payload["pedido"]
            sala_id = original["sala"]
            inicio = original["inicio"]
            fim = original["fim"]
            responsavel = original["responsavel"]
            error = validate(sala_id, inicio, fim)
            if error:
                return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":tool_error(error)})
            expected_key = payload["input_key"]
            if not isinstance(responses, dict) or set(responses) != {expected_key}:
                return JSONResponse(rpc_error(req_id, -32602, "Invalid elicitation input key"))
            response = responses[expected_key]
            action = response.get("action") if isinstance(response, dict) else None
            if action in {"decline","cancel"}:
                data={"reserva":None,"reservado":False,"sala":None,"inicio":None,"fim":None,"responsavel":None,"politica":None,"motivo":"recusado"}
                text=json.dumps(data,ensure_ascii=False,indent=2)
                return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":{"content":[{"type":"text","text":text}],"resultType":"complete","structuredContent":data,"_meta":{"serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}})
            chosen = (response or {}).get("content",{}).get("sala")
            alt = payload["alternativas"]
            if action != "accept" or chosen not in alt:
                return JSONResponse(rpc_error(req_id,-32602,"Invalid elicitation response"))
            sala_id = chosen

        else:
            responsavel = args.get("responsavel")

        cs = conflitos(sala_id, inicio, fim)
        if cs:
            alt = alternativas(sala_id, inicio, fim)
            if not alt:
                return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":tool_error("Sem alternativas disponiveis no intervalo")})
            capabilities = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
            elic = (capabilities.get("elicitation") or {})
            if not isinstance(elic.get("form"), dict):
                return JSONResponse(rpc_error(req_id,-32021,"Elicitation form capability required",{"requiredCapabilities":{"elicitation":{"form":{}}}}),status_code=400)
            key = "__main__:escolha_de_sala"
            schema={"type":"object","properties":{"sala":{"type":"string","enum":alt}},"required":["sala"]}
            state_payload={"exp":int(time.time())+900,"pedido":{"sala":sala_id,"inicio":inicio,"fim":fim,"responsavel":responsavel},"alternativas":alt,"input_key":key}
            data={"inputRequests":{key:{"method":"elicitation/create","params":{"mode":"form","message":"A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.","requestedSchema":schema}}},"requestState":seal(state_payload),"resultType":"input_required","_meta":{"serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}
            return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":data})

        global res_counter
        res_id = f"res-{res_counter:04d}"; res_counter += 1
        reserva={"id":res_id,"sala":sala_id,"inicio":inicio,"fim":fim,"responsavel":responsavel}
        reservas.append(reserva)
        data={"reserva":res_id,"reservado":True,"sala":sala_id,"inicio":inicio,"fim":fim,"responsavel":responsavel,"politica":POLITICA}
        text=json.dumps(data,ensure_ascii=False)
        return JSONResponse({"jsonrpc":"2.0","id":req_id,"result":{"content":[{"type":"text","text":text}],"structuredContent":data,"resultType":"complete","isError":False,"_meta":{"serverInfo":{"name":"central-de-salas","version":"1.0.0"}}}})

    return JSONResponse(rpc_error(req_id,-32601,"Method not found"))


app = Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"])] )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
