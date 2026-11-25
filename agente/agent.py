#!/usr/bin/env python3
from __future__ import annotations

import json, os, re, secrets, sys, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT=int(os.getenv("AGENT_PORT","7300"))
MCP_URL=os.getenv("MCP_URL","http://127.0.0.1:7301/mcp")
PROTOCOL="2026-07-28"
POLITICA=None
TOOLS=set()
TASKS={}
RPC_ID=0


def http_post(url, payload, headers=None):
    req=urllib.request.Request(url,data=json.dumps(payload).encode(),headers=headers or {},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=20) as r:
            return r.status,json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code,json.loads(e.read().decode())


def trace_id(traceparent):
    if not traceparent:
        return None
    parts=traceparent.split("-")
    return parts[1] if len(parts)>=3 else None


def make_trace(traceparent):
    tid=trace_id(traceparent)
    if not tid:
        return None
    return f"00-{tid}-{secrets.token_hex(8)}-01"


def next_id():
    global RPC_ID
    RPC_ID+=1
    return RPC_ID


def ensure_mcp(traceparent=None):
    global POLITICA,TOOLS
    if not TOOLS:
        body={"jsonrpc":"2.0","id":next_id(),"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":PROTOCOL,"io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{}}}}}}
        meta=body["params"]["_meta"]
        tp=make_trace(traceparent)
        if tp: meta["traceparent"]=tp
        status,resp=http_post(MCP_URL,body,{"Content-Type":"application/json","Accept":"application/json, text/event-stream","MCP-Protocol-Version":PROTOCOL,"Mcp-Method":"tools/list"})
        TOOLS={t["name"] for t in (resp.get("result") or {}).get("tools",[])}

        rid=next_id(); body={"jsonrpc":"2.0","id":rid,"method":"resources/read","params":{"uri":"politica://uso","_meta":{"io.modelcontextprotocol/protocolVersion":PROTOCOL,"io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{}}}}}}
        if traceparent:
            body["params"]["_meta"]["traceparent"]=make_trace(traceparent)
        status,resp=http_post(MCP_URL,body,{"Content-Type":"application/json","Accept":"application/json, text/event-stream","MCP-Protocol-Version":PROTOCOL,"Mcp-Method":"resources/read","Mcp-Name":"politica://uso"})
        text=((resp.get("result") or {}).get("contents") or [{}])[0].get("text","")
        if text:
            first=text.splitlines()[0]
            POLITICA=first.split(":",1)[1].strip() if ":" in first else first


def mcp_call(name,args,traceparent=None,input_responses=None,request_state=None):
    ensure_mcp(traceparent)
    params={"name":name,"arguments":args,"_meta":{"io.modelcontextprotocol/protocolVersion":PROTOCOL,"io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{}}}}}
    tp=make_trace(traceparent)
    if tp: params["_meta"]["traceparent"]=tp
    if input_responses is not None: params["inputResponses"]={k:v for k,v in input_responses.items()}
    if request_state is not None: params["requestState"]=request_state
    body={"jsonrpc":"2.0","id":next_id(),"method":"tools/call","params":params}
    status,resp=http_post(MCP_URL,body,{"Content-Type":"application/json","Accept":"application/json, text/event-stream","MCP-Protocol-Version":PROTOCOL,"Mcp-Method":"tools/call","Mcp-Name":name})
    return resp


def parse_pedido(text):
    values={}
    for part in re.findall(r'(\w+)=([^ ]+)',text):
        values[part[0]]=part[1]
    return values


def msg(text,task_id=None,context_id=None):
    d={"messageId":"msg-"+secrets.token_hex(6),"role":"ROLE_AGENT","parts":[{"text":""}],"parts":[{"text":text}]}
    d["parts"][0]={"text":text}
    if task_id: d["taskId"]=task_id
    if context_id: d["contextId"]=context_id
    return d


def task_payload(task):
    return {
        "id": task["id"],
        "contextId": task["contextId"],
        "status": task.get("status", {}),
        "history": task.get("history", []),
        "artifacts": task.get("artifacts", []),
    }


def add_history_user(task,text,message_id=None,task_id=None):
    m={"messageId":message_id or "msg-"+secrets.token_hex(6),"role":"ROLE_USER","parts":[{"text":text}]}
    if task_id: m["taskId"]="" # replaced below
    task["history"].append(m)


def set_status(task,state,message=None):
    task["status"]={"state":state}
    if message:
        task["status"]["message"]=message


CARD={
  "name":"Central de Salas","description":"Reserva salas de reuniao da Hill Valley Tech.",
  "provider":{"organization":"Hill Valley Tech","url":"https://hillvalley.example"},
  "version":"1.0.0",
  "supportedInterfaces":[{"url":f"http://127.0.0.1:{PORT}/a2a","protocolBinding":"JSONRPC","protocolVersion":"1.0"}],
  "capabilities":{"streaming":False,"pushNotifications":False,"extendedAgentCard":False},
  "defaultInputModes":["text/plain"],"defaultOutputModes":["text/plain"],
  "skills":[{"id":"reservar-sala","name":"Reservar sala","description":"Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.","tags":["salas","agenda"],"inputModes":["text/plain"],"outputModes":["text/plain"],"examples":["reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"]}]
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,format,*args):
        print("A2A "+format%args,file=sys.stderr,flush=True)

    def send_json(self,status,payload):
        data=json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if self.path=="/.well-known/agent-card.json":
            self.send_json(200,CARD)
        else:
            self.send_json(404,{"error":"not found"})

    def do_POST(self):
        if self.path!="/a2a":
            self.send_json(404,{"jsonrpc":"2.0","error":{"code":-32601,"message":"Method not found"}}); return
        length=int(self.headers.get("Content-Length","0")); body=json.loads(self.rfile.read(length).decode())
        rpc_id=body.get("id")
        method=body.get("method")
        params=body.get("params") or {}
        traceparent=self.headers.get("traceparent")

        if method=="GetTask":
            tid=params.get("id")
            task=TASKS.get(tid)
            if not task:
                self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"error":{"code":-32602,"message":"Task not found"}})
            else:
                self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"result":{"task":task_payload(task)}})
            return

        if method!="SendMessage":
            self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"error":{"code":-32601,"message":"Method not found"}}); return

        message=params.get("message") or {}
        text=" ".join(p.get("text","") for p in (message.get("parts") or []))
        tid=message.get("taskId")

        if tid:
            task=TASKS.get(tid)
            if not task:
                self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"error":{"code":-32602,"message":"Task not found"}}); return
            if task["status"]["state"] in {"TASK_STATE_COMPLETED","TASK_STATE_CANCELED","TASK_STATE_FAILED"}:
                self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"error":{"code":-32602,"message":"Task is terminal"}}); return
            if task["status"]["state"]=="TASK_STATE_INPUT_REQUIRED":
                task_traceparent = task.get("traceparent") or traceparent
                if traceparent:
                    task["traceparent"] = traceparent
                user={"messageId":message.get("messageId") or "msg-"+secrets.token_hex(6),"role":"ROLE_USER","parts":[{"text":text}],"taskId":tid,"contextId":task["contextId"]}
                task["history"].append(user)
                alternatives=task["alternatives"]
                if text=="escolha=recusar":
                    response=mcp_call(task["name"],task["arguments"],task_traceparent,task["input_responses_for_decline"],task["requestState"])
                    agent=msg("Reserva recusada.",tid,task["contextId"]); task["history"].append(agent); set_status(task,"TASK_STATE_CANCELED",agent)
                    self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"result":{"task":task_payload(task)}}); return
                chosen=text.removeprefix("escolha=") if text.startswith("escolha=") else ""
                if chosen not in alternatives:
                    agent=msg("alternativas: "+", ".join(alternatives),tid,task["contextId"]); task["history"].append(agent); set_status(task,"TASK_STATE_INPUT_REQUIRED",agent)
                    self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"result":{"task":task_payload(task)}}); return
                input_responses={task["input_key"]:{"action":"accept","content":{"sala":chosen}}}
                response=mcp_call(task["name"],task["arguments"],task_traceparent,input_responses,task["requestState"])
                result=response.get("result") or {}
                if result.get("isError"):
                    err=" ".join(p.get("text","") for p in result.get("content",[])); agent=msg(err,tid,task["contextId"]); task["history"].append(agent); set_status(task,"TASK_STATE_FAILED",agent)
                else:
                    data=result.get("structuredContent") or {}; data={**data,"politica":POLITICA or data.get("politica")}
                    data_text=json.dumps(data,ensure_ascii=False)
                    agent=msg(f"Reserva {data.get('reserva')} confirmada na sala-{chosen.removeprefix('sala-')}.",tid,task["contextId"])
                    # Preserve exact artifact shape and a useful confirmation; the room name is sala-<id> already.
                    agent["parts"][0]={"text":f"Reserva {data.get('reserva')} confirmada na sala-{data.get('sala','').removeprefix('sala-')}."}
                    task["history"].append(agent)
                    artifact={"artifactId":"art-"+secrets.token_hex(6),"name":"reserva","parts":[{"text":data_text}]}
                    task.setdefault("artifacts",[]).append(artifact); set_status(task,"TASK_STATE_COMPLETED",agent)
                self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"result":{"task":task_payload(task)}}); return

        values=parse_pedido(text)
        if not all(k in values for k in ("sala","inicio","fim","responsavel")):
            tid2="task-"+secrets.token_hex(6); ctx="ctx-"+secrets.token_hex(6)
            task={"id":tid2,"contextId":ctx,"status":{"state":"TASK_STATE_FAILED"},"history":[],"artifacts":[]}
            user={"messageId":"msg-"+secrets.token_hex(6),"role":"ROLE_USER","parts":[{"text":text}]}; task["history"].append(user)
            agent=msg("Pedido invalido: formato esperado reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",tid2,ctx); task["history"].append(agent); set_status(task,"TASK_STATE_FAILED",agent); TASKS[tid2]=task
            self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"result":{"task":task_payload(task)}}); return

        tid="task-"+secrets.token_hex(6); ctx="ctx-"+secrets.token_hex(6)
        task={"id":tid,"contextId":ctx,"status":{"state":"TASK_STATE_SUBMITTED"},"history":[],"artifacts":[],"traceparent":traceparent}
        user={"messageId":message.get("messageId") or "msg-"+secrets.token_hex(6),"role":"ROLE_USER","parts":[{"text":text}]}; task["history"].append(user); TASKS[tid]=task
        set_status(task,"TASK_STATE_WORKING")
        args={"sala":values["sala"],"inicio":values["inicio"],"fim":values["fim"],"responsavel":values["responsavel"]}
        response=mcp_call("reservar_sala",args,traceparent)
        result=response.get("result") or {}
        if result.get("resultType")=="input_required":
            requests=result.get("inputRequests") or {}; key=next(iter(requests),None); state=result.get("requestState")
            params_req=(requests.get(key) or {}).get("params") or {}; schema=((params_req.get("requestedSchema") or {}).get("properties") or {}).get("sala") or {}
            alternatives=schema.get("enum") or ([schema["const"]] if "const" in schema else [])
            task.update({"name":"reservar_sala","arguments":args,"requestState":state,"input_key":key,"alternatives":alternatives,"input_responses_for_decline":{key:{"action":"decline"}}})
            agent=msg("alternativas: "+", ".join(alternatives),tid,ctx); task["history"].append(agent); set_status(task,"TASK_STATE_INPUT_REQUIRED",agent)
        elif result.get("isError"):
            err=" ".join(p.get("text","") for p in result.get("content",[])); agent=msg(err,tid,ctx); task["history"].append(agent); set_status(task,"TASK_STATE_FAILED",agent)
        else:
            data=result.get("structuredContent") or {}; data={**data,"politica":POLITICA or data.get("politica")}
            agent=msg(f"Reserva {data.get('reserva')} confirmada na sala-{data.get('sala','').removeprefix('sala-')}.",tid,ctx)
            task["history"].append(agent)
            artifact={"artifactId":"art-"+secrets.token_hex(6),"name":"reserva","parts":[{"text":json.dumps(data,ensure_ascii=False)}]}; task["artifacts"]=[artifact]; set_status(task,"TASK_STATE_COMPLETED",agent)
        self.send_json(200,{"jsonrpc":"2.0","id":rpc_id,"result":{"task":task_payload(task)}})


if __name__=="__main__":
    server=ThreadingHTTPServer(("0.0.0.0",PORT),Handler)
    print(f"A2A listening on {PORT}",file=sys.stderr,flush=True)
    server.serve_forever()
