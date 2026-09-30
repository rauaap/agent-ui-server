import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from agent_ui_server import model_catalog
from agent_ui_server.agent import ClaudeCodeAdapter, PiAdapter
from agent_ui_server.db import Database
from agent_ui_server.session_tools import validate_session_arguments


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_script(self, agent, script):
        original = asyncio.create_subprocess_exec
        async def spawn(*args, **kwargs):
            return await original(sys.executable, "-c", script, **kwargs)
        with patch("agent_ui_server.model_catalog.asyncio.create_subprocess_exec", spawn):
            return await model_catalog.discover_models(agent, "fake")

    async def test_claude_initialization_only(self):
        script = '''import json,sys
request=json.loads(sys.stdin.readline())
assert request['request']['subtype']=='initialize'
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':{'models':[{'value':'default','resolvedModel':'claude-opus-5-5','displayName':'Default (recommended)'},{'value':'opus','resolvedModel':'claude-opus-5-5','displayName':'Opus 5.5'},{'value':'claude-opus-5-5','resolvedModel':'claude-opus-5-5','displayName':'Duplicate'},{'value':'sonnet','resolvedModel':'claude-sonnet-5-5','displayName':'Sonnet 5.5'}]}}}),flush=True)
sys.stdin.read()
'''
        self.assertEqual(await self.run_script("claude-code", script), {
            "models": [{"id": "claude-opus-5-5", "name": "Opus 5.5"},
                       {"id": "claude-sonnet-5-5", "name": "Sonnet 5.5"}], "error": None})

    async def test_claude_missing_concrete_id_does_not_fall_back(self):
        script = '''import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':{'models':[{'value':'opus','displayName':'Opus'}]}}}),flush=True)
sys.stdin.read()
'''
        result = await self.run_script("claude-code", script)
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result["error"])

    async def test_pi_table(self):
        result = await self.run_script("pi", "print('provider model context max-out thinking images\\nopenai old-model 128K 32K yes yes\\nopenai new-model 128K 32K yes yes')")
        self.assertEqual(result, {"models": [
            {"id": "openai/new-model", "name": "new-model"},
            {"id": "openai/old-model", "name": "old-model"},
        ], "error": None})

    async def test_failure_and_timeout_are_isolated(self):
        result = await self.run_script("pi", "import sys;sys.exit(2)")
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result["error"])
        with patch.object(model_catalog, "DISCOVERY_TIMEOUT", 0.05):
            result = await self.run_script("claude-code", "import time;time.sleep(10)")
        self.assertIsNotNone(result["error"])
        with patch.object(model_catalog, "_claude_models", AsyncMock(side_effect=RuntimeError("bad"))), patch.object(model_catalog, "_pi_models", AsyncMock(return_value=[])):
            result = await model_catalog.discover_catalog({"claude-code": ClaudeCodeAdapter(), "pi": PiAdapter()})
        self.assertIsNotNone(result["claude-code"]["error"])
        self.assertIsNone(result["pi"]["error"])

    async def test_launches_use_saved_model_on_resume(self):
        for adapter, resume_flag in [(ClaudeCodeAdapter(), "--resume"), (PiAdapter(), "--session")]:
            commands = []
            async def spawn(*args, **kwargs):
                commands.append(args)
                raise FileNotFoundError("Probe stops after capturing launch arguments")
            with tempfile.TemporaryDirectory() as cwd, patch("agent_ui_server.agent.asyncio.create_subprocess_exec", spawn):
                _ = [e async for e in adapter.start_turn({"id": 1, "working_dir": cwd, "sandbox": False, "agent_session_id": "resume-id", "model": "saved-model"}, "hi")]
            command = commands[0]
            self.assertEqual(command[command.index("--model") + 1], "saved-model")
            self.assertEqual(command[command.index(resume_flag) + 1], "resume-id")


class SessionModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from agent_ui_server import main
        self.main = main
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "db.sqlite"
        self.db = Database(self.path)
        self.project = self.db.create_project(self.tmp.name, "project")
        self.patch_db = patch.object(main, "db", self.db)
        self.patch_db.start()
        self.catalog = {"pi": {"models": [{"id": "p/m", "name": "M"}], "error": None}, "claude-code": {"models": [], "error": "Unavailable"}}
        self.patch_catalog = patch.object(main, "model_catalog", self.catalog)
        self.patch_catalog.start()

    def tearDown(self):
        self.patch_catalog.stop()
        self.patch_db.stop()
        self.db.close()
        self.tmp.cleanup()

    async def test_create_list_persist_and_default(self):
        session = await self.main.create_session(self.main.CreateSessionRequest(name="test", project_path=self.tmp.name, agent="pi", model="p/m"))
        self.assertEqual(session["model"], "p/m")
        self.assertEqual((await self.main.list_sessions())[0]["model"], "p/m")
        reopened = Database(self.path)
        try:
            self.assertEqual(reopened.get_session(session["id"])["model"], "p/m")
        finally:
            reopened.close()
        default = await self.main.create_session(self.main.CreateSessionRequest(name="default", project_path=self.tmp.name))
        self.assertIsNone(default["model"])

    async def test_validation_and_no_refresh(self):
        for agent, model, status in [("pi", "wrong", 400), ("claude-code", "opus", 503)]:
            with self.assertRaises(HTTPException) as caught:
                await self.main.create_session(self.main.CreateSessionRequest(name="test", project_path=self.tmp.name, agent=agent, model=model))
            self.assertEqual(caught.exception.status_code, status)
        with patch.object(self.main, "discover_catalog", AsyncMock()) as discover:
            expected = [
                {"id": "claude-code", "name": "Claude Code", "default": True,
                 "models": [], "models_error": "Unavailable"},
                {"id": "pi", "name": "Pi", "default": False,
                 "models": [{"id": "p/m", "name": "M"}], "models_error": None},
            ]
            self.assertEqual(await self.main.list_agents(), expected)
            self.assertEqual(await self.main.list_agents(), expected)
            discover.assert_not_called()
        with self.assertRaises(ValidationError):
            self.main.UpdateSessionRequest(model="p/m")
        self.assertEqual(validate_session_arguments("start_session", {"name": "n", "project_path": "/p", "message": "hi", "model": "p/m"})["model"], "p/m")

    async def test_empty_catalog_and_model_order(self):
        self.catalog["claude-code"] = {"models": [], "error": None}
        self.catalog["pi"]["models"].append({"id": "p/second", "name": "Second"})
        agents = await self.main.list_agents()
        self.assertEqual(agents[0]["models"], [])
        self.assertIsNone(agents[0]["models_error"])
        self.assertEqual(agents[1]["models"], self.catalog["pi"]["models"])

    def test_openapi_catalog_schema_and_removed_models_endpoint(self):
        schema = self.main.app.openapi()
        self.assertNotIn("/models", schema["paths"])
        response = schema["paths"]["/agents"]["get"]["responses"]["200"]
        items = response["content"]["application/json"]["schema"]["items"]
        self.assertEqual(items["$ref"], "#/components/schemas/CatalogAgent")
        agent = schema["components"]["schemas"]["CatalogAgent"]
        self.assertEqual(set(agent["required"]), {"id", "name", "default", "models", "models_error"})
        self.assertEqual(agent["properties"]["models"]["items"]["$ref"], "#/components/schemas/CatalogModel")

    async def test_startup_discovers_once(self):
        with patch.object(self.main, "load_auth_token"), patch.object(self.main, "discover_catalog", AsyncMock(return_value=self.catalog)) as discover:
            await self.main.startup()
            discover.assert_awaited_once_with(self.main.adapters)

    def test_legacy_database_migration(self):
        self.db._conn.execute("ALTER TABLE sessions DROP COLUMN model")
        self.db._conn.commit()
        migrated = Database(self.path)
        try:
            session = migrated.create_session("legacy", self.project["id"], "pi")
            self.assertIsNone(session["model"])
        finally:
            migrated.close()
