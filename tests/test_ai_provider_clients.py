import os
import unittest

from src.core.config.config_env import resolve_env_placeholder, resolve_string_values_in_data


class ResolveEnvPlaceholderTests(unittest.TestCase):
    def test_plain_string_keeps_original_value(self):
        self.assertEqual(resolve_env_placeholder("abc123"), "abc123")

    def test_resolve_env_placeholder_string(self):
        old = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "env-secret"
        try:
            self.assertEqual(resolve_env_placeholder("${OPENAI_API_KEY}"), "env-secret")
        finally:
            if old is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old

    def test_missing_env_name_returns_empty_string(self):
        self.assertEqual(resolve_env_placeholder("${NOT_EXISTS_FOR_TEST}"), "")


class ResolveStringValuesInDataTests(unittest.TestCase):
    def test_resolve_all_string_params_recursively(self):
        old_key = os.environ.get("OPENAI_API_KEY")
        old_base = os.environ.get("OPENAI_BASE_URL")
        os.environ["OPENAI_API_KEY"] = "token-1"
        os.environ["OPENAI_BASE_URL"] = "https://example.com/v1"
        try:
            raw = {
                "api_key": "${OPENAI_API_KEY}",
                "base_url": "${OPENAI_BASE_URL}",
                "model": "gpt-4o-mini",
                "nested": {
                    "headers": ["Bearer ${OPENAI_API_KEY}", "${OPENAI_API_KEY}"],
                    "timeout": 120,
                },
            }
            got = resolve_string_values_in_data(raw)
            self.assertEqual(got["api_key"], "token-1")
            self.assertEqual(got["base_url"], "https://example.com/v1")
            self.assertEqual(got["model"], "gpt-4o-mini")
            self.assertEqual(got["nested"]["headers"][0], "Bearer ${OPENAI_API_KEY}")
            self.assertEqual(got["nested"]["headers"][1], "token-1")
            self.assertEqual(got["nested"]["timeout"], 120)
        finally:
            if old_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = old_key
            if old_base is None:
                os.environ.pop("OPENAI_BASE_URL", None)
            else:
                os.environ["OPENAI_BASE_URL"] = old_base

    def test_resolve_full_config_style_string_fields(self):
        old_provider = os.environ.get("MODEL_PROVIDER")
        old_policy = os.environ.get("EXEC_POLICY")
        old_model = os.environ.get("MODEL_NAME")
        os.environ["MODEL_PROVIDER"] = "openai"
        os.environ["EXEC_POLICY"] = "moderate"
        os.environ["MODEL_NAME"] = "gpt-4o-mini"
        try:
            raw = {
                "execution_policy": "${EXEC_POLICY}",
                "model_providers": [
                    {
                        "provider": "${MODEL_PROVIDER}",
                        "params": {
                            "models": ["${MODEL_NAME}"],
                        },
                    }
                ],
            }
            got = resolve_string_values_in_data(raw)
            self.assertEqual(got["execution_policy"], "moderate")
            self.assertEqual(got["model_providers"][0]["provider"], "openai")
            self.assertEqual(got["model_providers"][0]["params"]["models"][0], "gpt-4o-mini")
        finally:
            if old_provider is None:
                os.environ.pop("MODEL_PROVIDER", None)
            else:
                os.environ["MODEL_PROVIDER"] = old_provider
            if old_policy is None:
                os.environ.pop("EXEC_POLICY", None)
            else:
                os.environ["EXEC_POLICY"] = old_policy
            if old_model is None:
                os.environ.pop("MODEL_NAME", None)
            else:
                os.environ["MODEL_NAME"] = old_model

    def test_resolve_non_string_types_from_env(self):
        old_int = os.environ.get("CFG_INT")
        old_float = os.environ.get("CFG_FLOAT")
        old_bool = os.environ.get("CFG_BOOL")
        old_null = os.environ.get("CFG_NULL")
        old_list = os.environ.get("CFG_LIST")
        old_obj = os.environ.get("CFG_OBJ")
        old_yes = os.environ.get("CFG_YES")
        os.environ["CFG_INT"] = "42"
        os.environ["CFG_FLOAT"] = "3.5"
        os.environ["CFG_BOOL"] = "true"
        os.environ["CFG_NULL"] = "null"
        os.environ["CFG_LIST"] = "[1,2,3]"
        os.environ["CFG_OBJ"] = "{\"a\":1}"
        os.environ["CFG_YES"] = "yes"
        try:
            raw = {
                "int_v": "${CFG_INT}",
                "float_v": "${CFG_FLOAT}",
                "bool_v": "${CFG_BOOL}",
                "null_v": "${CFG_NULL}",
                "list_v": "${CFG_LIST}",
                "obj_v": "${CFG_OBJ}",
                "yes_v": "${CFG_YES}",
            }
            got = resolve_string_values_in_data(raw)
            self.assertEqual(got["int_v"], 42)
            self.assertEqual(got["float_v"], 3.5)
            self.assertEqual(got["bool_v"], True)
            self.assertEqual(got["null_v"], None)
            self.assertEqual(got["list_v"], [1, 2, 3])
            self.assertEqual(got["obj_v"], {"a": 1})
            self.assertEqual(got["yes_v"], True)
        finally:
            if old_int is None:
                os.environ.pop("CFG_INT", None)
            else:
                os.environ["CFG_INT"] = old_int
            if old_float is None:
                os.environ.pop("CFG_FLOAT", None)
            else:
                os.environ["CFG_FLOAT"] = old_float
            if old_bool is None:
                os.environ.pop("CFG_BOOL", None)
            else:
                os.environ["CFG_BOOL"] = old_bool
            if old_null is None:
                os.environ.pop("CFG_NULL", None)
            else:
                os.environ["CFG_NULL"] = old_null
            if old_list is None:
                os.environ.pop("CFG_LIST", None)
            else:
                os.environ["CFG_LIST"] = old_list
            if old_obj is None:
                os.environ.pop("CFG_OBJ", None)
            else:
                os.environ["CFG_OBJ"] = old_obj
            if old_yes is None:
                os.environ.pop("CFG_YES", None)
            else:
                os.environ["CFG_YES"] = old_yes


if __name__ == "__main__":
    unittest.main()
