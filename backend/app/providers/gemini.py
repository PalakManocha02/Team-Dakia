"""Gemini REST structured output; existing Python/httpx stack, no JS service needed.

Contracts: https://ai.google.dev/api/generate-content
JSON schema: https://ai.google.dev/gemini-api/docs/structured-output
"""
import hashlib
import json
import os
from pathlib import Path
import re
import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine
from app.providers.interfaces import IntegrationUnavailable
from app.providers.vertex_gemini import VertexGeminiTransport, validate_gemini_model


class GeminiClient:
    def __init__(self, key=None, model=None, journal_path="gemini-calls.db", client=None, max_calls=4,
                 backend=None, credentials_file=None, project=None, location=None, vertex_client=None):
        # Vertex AI (service-account JSON) when GOOGLE_APPLICATION_CREDENTIALS is set; else the AI Studio key.
        self.backend = backend or os.getenv("GEMINI_BACKEND") or ("vertex" if credentials_file or vertex_client or (client is None and os.getenv("GOOGLE_APPLICATION_CREDENTIALS")) else "developer")
        if self.backend not in {"vertex", "developer"}:
            raise IntegrationUnavailable("GEMINI_ONLY: backend must be vertex or developer")
        self.model = model or os.getenv("GEMINI_MODEL") or ("gemini-2.5-flash" if self.backend == "vertex" else "gemini-3.1-flash-lite")
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", self.model):
            raise IntegrationUnavailable("GEMINI_INVALID_MODEL")
        if self.backend == "vertex":
            validate_gemini_model(self.model)
        self.max_calls = max_calls
        if self.backend == "vertex":
            self.http = VertexGeminiTransport(credentials_file=credentials_file, project=project, location=location, sdk_client=vertex_client)
        else:
            key = key or os.getenv("GEMINI_API_KEY")
            if not key and client is None:
                raise IntegrationUnavailable("GEMINI_NOT_CONFIGURED: set GEMINI_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS for Vertex AI")
            self.http = client or httpx.Client(base_url="https://generativelanguage.googleapis.com", headers={"x-goog-api-key":key}, timeout=45, follow_redirects=False)
        self.engine = create_engine("sqlite:///"+str(Path(journal_path).resolve()), connect_args={"timeout":30, "check_same_thread":False})
        with self.engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE IF NOT EXISTS generations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL, model TEXT NOT NULL, state TEXT NOT NULL, response TEXT, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER)")

    def generate(self, run_id: str, stage: str, system: str, data: dict, schema: type[BaseModel]):
        content = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if len(content) > 60000:
            raise IntegrationUnavailable("GEMINI_INPUT_LIMIT: extraction input exceeds 60000 characters")
        contract = schema.model_json_schema()
        key = hashlib.sha256(json.dumps([self.model,stage,system,content,contract],sort_keys=True).encode()).hexdigest()
        call_id = f"{run_id}:{key}"
        with self.engine.connect() as c:
            c.exec_driver_sql("BEGIN IMMEDIATE")
            prior = c.exec_driver_sql("SELECT state,response FROM generations WHERE id=?",(call_id,)).first()
            if prior:
                c.rollback()
                if prior[0] == "succeeded": return schema.model_validate_json(prior[1])
                raise IntegrationUnavailable("GEMINI_PRIOR_CALL_UNRESOLVED: no automatic regeneration after a failed or ambiguous call")
            count = c.exec_driver_sql("SELECT COUNT(*) FROM generations WHERE run_id=?",(run_id,)).scalar_one()
            if count >= self.max_calls:
                c.rollback()
                raise IntegrationUnavailable("GEMINI_CALL_LIMIT: maximum 4 generations per run, including clarification attempts")
            c.exec_driver_sql("INSERT INTO generations VALUES(?,?,?,?,'pending',NULL,NULL,NULL,NULL)",(call_id,run_id,stage,self.model))
            c.commit()
        payload = {"systemInstruction":{"parts":[{"text":system}]},
                   "contents":[{"role":"user","parts":[{"text":content}]}],
                   "generationConfig":{"responseMimeType":"application/json", "responseJsonSchema":contract, "maxOutputTokens":4096}}
        try:
            response = self.http.post(f"/v1beta/models/{self.model}:generateContent",json=payload)
        except httpx.TransportError:
            raise IntegrationUnavailable("GEMINI_NETWORK_ERROR: generation outcome unknown; no automatic paid retry") from None
        usage = {}
        result = None
        error = f"GEMINI_HTTP_{response.status_code}"
        if response.status_code == 200:
            try:
                body = response.json()
                usage = body.get("usageMetadata", {})
                candidate = body.get("candidates", [])[0]
                if candidate.get("finishReason") != "STOP":
                    raise ValueError("incomplete or refused generation")
                parts = candidate.get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts if not p.get("thought", False))
                result = schema.model_validate_json(text)
            except (ValueError, ValidationError, KeyError, IndexError, TypeError, AttributeError):
                error = "GEMINI_INVALID_OR_BLOCKED_OUTPUT: no extraction applied"
        def tokens(name):
            value = usage.get(name) if isinstance(usage,dict) else None
            return value if type(value) is int and value >= 0 else None
        with self.engine.begin() as c:
            c.exec_driver_sql("UPDATE generations SET state=?,response=?,input_tokens=?,output_tokens=?,total_tokens=? WHERE id=?",
                ("succeeded" if result is not None else "failed", result.model_dump_json() if result is not None else None,
                 tokens("promptTokenCount"),tokens("candidatesTokenCount"),tokens("totalTokenCount"),call_id))
        if result is None:
            raise IntegrationUnavailable(error+"; provider response body omitted")
        return result

    def usage(self, run_id):
        with self.engine.connect() as c:
            return [dict(r) for r in c.exec_driver_sql("SELECT id,stage,model,state,input_tokens,output_tokens,total_tokens FROM generations WHERE run_id=?",(run_id,)).mappings()]
