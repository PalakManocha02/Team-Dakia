"""Google Gemini only, through Vertex AI as specified by the supplied access README."""
import json
import os
from pathlib import Path
import re
import httpx
from app.providers.interfaces import IntegrationUnavailable


def validate_gemini_model(model):
    # Reject publisher/resource paths and all partner or other Google model families.
    if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", model):
        raise IntegrationUnavailable("GEMINI_ONLY: only bare Google gemini-* model IDs are permitted")


def vertex_schema(contract):
    """Translate Pydantic JSON Schema to Vertex's supported responseSchema subset.

    Keep full Pydantic validation locally; omit unsupported defaults, extra-property
    constraints and length hints from the generation schema. No model request needed.
    """
    definitions = contract.get("$defs",{})
    def convert(node):
        if "$ref" in node:
            node = definitions[node["$ref"].split("/")[-1]]
        if "anyOf" in node:
            choices = [choice for choice in node["anyOf"] if choice.get("type") != "null"]
            nullable = len(choices) != len(node["anyOf"])
            result = convert(choices[0]) if len(choices)==1 else {"anyOf":[convert(choice) for choice in choices]}
            if nullable: result["nullable"] = True
            return result
        result = {}
        if "type" in node: result["type"] = node["type"].upper()
        for key in ["enum","required","description","minimum","maximum"]:
            if key in node: result[key] = node[key]
        if "properties" in node: result["properties"] = {k:convert(v) for k,v in node["properties"].items()}
        if "items" in node: result["items"] = convert(node["items"])
        return result
    return convert(contract)


class VertexGeminiTransport:
    def __init__(self, credentials_file=None, project=None, location=None, sdk_client=None):
        self.location = location or os.getenv("GOOGLE_CLOUD_LOCATION", "global")
        self.project = project or os.getenv("GOOGLE_CLOUD_PROJECT")
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise IntegrationUnavailable("GEMINI_VERTEX_DEPENDENCY_MISSING: install backend dependencies including google-genai") from None
        self.types = types
        if sdk_client is not None:
            self.sdk = sdk_client
            return
        path = credentials_file or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
        if not path:
            raise IntegrationUnavailable("GEMINI_NOT_CONFIGURED: set GOOGLE_APPLICATION_CREDENTIALS to the external service-account JSON file")
        try:
            credential_path = Path(path).expanduser().resolve()
            info = json.loads(credential_path.read_text(encoding="utf-8"))
            credential_project = info.get("project_id")
            if info.get("type") != "service_account" or not credential_project:
                raise ValueError("not a project service-account credential")
            if self.project and self.project != credential_project:
                raise ValueError("configured project differs from the credential project")
            self.project = credential_project
            from google.oauth2 import service_account
            credentials = service_account.Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except (OSError, ValueError, TypeError, KeyError):
            raise IntegrationUnavailable("GEMINI_VERTEX_CREDENTIAL_ERROR: external service-account file is unreadable, invalid, or belongs to a different project") from None
        self.sdk = genai.Client(vertexai=True, credentials=credentials, project=self.project, location=self.location,
            http_options=types.HttpOptions(api_version="v1", timeout=45000, retry_options=types.HttpRetryOptions(attempts=1)))

    def post(self, path, json):
        # The existing extraction client has exactly one allowed generation operation.
        match = re.fullmatch(r"/v1beta/models/(gemini-[a-zA-Z0-9._-]+):generateContent", path)
        if not match:
            raise IntegrationUnavailable("GEMINI_ONLY: unsupported model operation")
        model = match[1]
        validate_gemini_model(model)
        generation = json["generationConfig"]
        config = self.types.GenerateContentConfig(
            system_instruction=json["systemInstruction"]["parts"][0]["text"],
            response_mime_type="application/json",
            response_schema=vertex_schema(generation["responseJsonSchema"]),
            max_output_tokens=generation["maxOutputTokens"],
            automatic_function_calling=self.types.AutomaticFunctionCallingConfig(disable=True),
        )
        if model.startswith("gemini-2.5-flash"):
            config.thinking_config = self.types.ThinkingConfig(thinking_budget=0)
        try:
            result = self.sdk.models.generate_content(model=model, contents=json["contents"], config=config)
            return httpx.Response(200, json=result.model_dump(mode="json", by_alias=True, exclude_none=True))
        except Exception as exc:
            # Never surface SDK error bodies, tokens, private-key material or credential paths.
            from google.genai.errors import APIError
            if isinstance(exc, APIError):
                code = exc.code if type(exc.code) is int and 400 <= exc.code <= 599 else 502
                return httpx.Response(code, json={"error":"Vertex Gemini generation failed"})
            raise httpx.TransportError("Vertex Gemini authentication or generation transport failed") from None

    def close(self):
        self.sdk.close()
