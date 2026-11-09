"""Контракт API v2: api/v2.yaml валиден, а КАЖДЫЙ ответ живого сервиса во всех сценариях test_v2_http проходит схему
и имеет описанный для операции HTTP-код. Те же 26 сценариев, но клиент проверяет ответы по спецификации.

    TIPS_HMAC_SECRET=<демо-ключ> python3 -m unittest tests.test_v2_contract
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_v2_http as base  # noqa: E402  (модуль, а не имя класса: иначе discover запустит сценарии дважды)
from http_support import HAVE_DEPS, SECRET  # noqa: E402

try:
    import jsonschema
    import yaml
except ImportError:  # pragma: no cover
    jsonschema = yaml = None

SPEC = os.path.join(HERE, "..", "api", "v2.yaml")


def resolve(node, spec):
    """Подставляет $ref и приводит OpenAPI 3.0 (nullable) к виду, понятному валидатору."""
    if isinstance(node, list):
        return [resolve(x, spec) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        return resolve(target, spec)
    out = {k: resolve(v, spec) for k, v in node.items() if k not in ("example", "nullable")}
    if node.get("nullable"):
        out = {"anyOf": [out, {"type": "null"}]}
    return out


class Contract:
    def __init__(self):
        with open(SPEC, encoding="utf-8") as f:
            self.spec = yaml.safe_load(f)
        self.routes = []
        for path, ops in self.spec["paths"].items():
            self.routes.append((re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path) + "$"), path, ops))
        self.checked = 0

    def find(self, path, method):
        for rx, template, ops in self.routes:
            if rx.match(path) and method.lower() in ops:
                return template, ops[method.lower()]
        return None, None

    def verify(self, response):
        request = response.request
        path = request.url.path
        if not path.startswith("/api/v2/"):
            return  # вход и отчёты описаны в контракте v1
        template, op = self.find(path, request.method)
        if op is None:
            assert response.status_code in (404, 405, 400), f"{request.method} {path}: нет в контракте ({response.status_code})"
            return
        responses = resolve(op["responses"], self.spec)
        code = str(response.status_code)
        assert code in responses, f"{request.method} {template}: код {code} не описан (описаны {sorted(responses)})"
        content = responses[code].get("content")
        if content and response.content:
            schema = content["application/json"]["schema"]
            errors = sorted(jsonschema.Draft4Validator(schema).iter_errors(response.json()), key=lambda e: list(e.path))
            assert not errors, f"{request.method} {template} {code}: {errors[0].message} в {list(errors[0].path)}"
        for header in (responses[code].get("headers") or {}):
            assert header.lower() in response.headers, f"{request.method} {template} {code}: нет заголовка {header}"
        self.checked += 1


class CheckedClient:
    def __init__(self, client, contract):
        self._client, self._contract = client, contract

    def request(self, method, url, **kw):
        response = self._client.request(method, url, **kw)
        self._contract.verify(response)
        return response

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def __getattr__(self, name):
        return getattr(self._client, name)


CONTRACT = Contract() if yaml else None


@unittest.skipUnless(HAVE_DEPS and SECRET and yaml, "нужны зависимости сервиса, pyyaml, jsonschema и TIPS_HMAC_SECRET")
class V2ContractTests(base.V2Tests):
    def setUp(self):
        super().setUp()
        self.client = CheckedClient(self.client, CONTRACT)


@unittest.skipUnless(yaml, "нужны pyyaml и jsonschema")
class SpecTests(unittest.TestCase):
    def test_spec_is_valid_openapi(self):
        try:
            from openapi_spec_validator import validate_spec
        except ImportError:
            self.skipTest("нет openapi-spec-validator")
        validate_spec(CONTRACT.spec)

    def test_every_documented_operation_has_error_catalog_codes_only(self):
        from tips.domain.errors import ERRORS
        listed = set(CONTRACT.spec["components"]["schemas"]["Error"]["properties"]["error_code"]["enum"])
        self.assertEqual(listed, set(ERRORS))

    def test_money_pattern_rejects_exponent_and_trailing_newline(self):
        pattern = re.compile(CONTRACT.spec["components"]["schemas"]["Money"]["pattern"])
        for ok in ("0.00", "637.00", "9999999.99"):
            self.assertTrue(pattern.match(ok), ok)
        for bad in ("1e2", "1.5", "01.00", " 1.00", "-1.00", "10000000.00", "1,00"):
            self.assertFalse(pattern.match(bad), bad)


if __name__ == "__main__":
    unittest.main()
