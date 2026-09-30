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
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':{'models':[{'value':'default','resolvedModel':'claude-opus-5-5','displayName':'Default (recommended)'},{'value':'opus','resolvedModel':'claude-opus-5-5','displayName':'Opus 5.5','supportedEffortLevels':['low','max']},{'value':'claude-opus-5-5','resolvedModel':'claude-opus-5-5','displayName':'Duplicate'},{'value':'haiku','resolvedModel':'claude-haiku-4-5','displayName':'Haiku 4.5'}]}}}),flush=True)
sys.stdin.read()
'''
        self.assertEqual(await self.run_script("claude-code", script), {
            "models": [{"id": "claude-opus-5-5", "name": "Opus 5.5", "reasoning_levels": ["low", "max"]},
                       {"id": "claude-haiku-4-5", "name": "Haiku 4.5", "reasoning_levels": []}], "error": None})

    async def test_claude_missing_concrete_id_does_not_fall_back(self):
        script = '''import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{'subtype':'success','request_id':request['request_id'],'response':{'models':[{'value':'opus','displayName':'Opus'}]}}}),flush=True)
sys.stdin.read()
'''
        result = await self.run_script("claude-code", script)
        self.assertEqual(result["models"], [])
        self.assertIsNotNone(result["error"])

    async def test_pi_rpc_metadata_only(self):
        script = '''import json,sys
models=[{'provider':'openai','id':'old-model'},{'provider':'anthropic','id':'z-model'},{'provider':'openai','id':'new-model'}]
levels={'old-model':['off','low'],'new-model':['low','high','max'],'z-model':['off']}
selected=None
print(json.dumps({'type':'extension_ui_request','id':'x'}),flush=True)
for line in sys.stdin:
    command=json.loads(line)
    kind=command['type']
    assert kind in ('get_available_models','set_model','get_available_thinking_levels'),kind
    data=None
    if kind=='get_available_models':
        data={'models':models}
    elif kind=='set_model':
        selected=command['modelId']
    else:
        data={'levels':levels[selected]}
    print(json.dumps({'type':'response','id':command['id'],'command':kind,'success':True,'data':data}),flush=True)
'''
        self.assertEqual(await self.run_script("pi", script), {"models": [
            {"id": "openai/old-model", "name": "old-model", "reasoning_levels": ["off", "low"]},
            {"id": "openai/new-model", "name": "new-model", "reasoning_levels": ["low", "high", "max"]},
            {"id": "anthropic/z-model", "name": "z-model", "reasoning_levels": ["off"]},
        ], "error": None})

    async def test_pi_rpc_error_response(self):
        script = '''import json,sys
command=json.loads(sys.stdin.readline())
print(json.dumps({'type':'response','id':command['id'],'command':command['type'],'success':False,'error':'No models'}),flush=True)
sys.stdin.read()
'''
        self.assertEqual(await self.run_script("pi", script), {"models": [], "error": "Model discovery failed: No models"})

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

    async def launch_command(self, adapter, session):
        commands = []
        async def spawn(*args, **kwargs):
            commands.append(args)
            raise FileNotFoundError("Probe stops after capturing launch arguments")
        with tempfile.TemporaryDirectory() as cwd, patch("agent_ui_server.agent.asyncio.create_subprocess_exec", spawn):
            _ = [e async for e in adapter.start_turn({"id": 1, "working_dir": cwd, "sandbox": False, **session}, "hi")]
        return commands[0]

    async def test_launches_use_saved_model_and_reasoning_level_on_resume(self):
        for adapter, resume_flag, level_flag in [
            (ClaudeCodeAdapter(), "--resume", "--effort"), (PiAdapter(), "--session", "--thinking"),
        ]:
            command = await self.launch_command(adapter, {
                "agent_session_id": "resume-id", "model": "saved-model", "reasoning_level": "high",
            })
            self.assertEqual(command[command.index("--model") + 1], "saved-model")
            self.assertEqual(command[command.index(level_flag) + 1], "high")
            self.assertEqual(command[command.index(resume_flag) + 1], "resume-id")
            command = await self.launch_command(adapter, {"model": None, "reasoning_level": None})
            self.assertNotIn("--model", command)
            self.assertNotIn(level_flag, command)


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
        self.catalog = {"pi": {"models": [{"id": "p/m", "name": "M", "reasoning_levels": ["low", "high"]}], "error": None}, "claude-code": {"models": [], "error": "Unavailable"}}
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
                 "models": [{"id": "p/m", "name": "M", "reasoning_levels": ["low", "high"]}],
                 "models_error": None},
            ]
            self.assertEqual(await self.main.list_agents(), expected)
            self.assertEqual(await self.main.list_agents(), expected)
            discover.assert_not_called()
        with self.assertRaises(ValidationError):
            self.main.UpdateSessionRequest(model="p/m")
        self.assertEqual(validate_session_arguments("start_session", {"name": "n", "project_path": "/p", "message": "hi", "model": "p/m"})["model"], "p/m")

    async def test_empty_catalog_and_model_order(self):
        self.catalog["claude-code"] = {"models": [], "error": None}
        self.catalog["pi"]["models"].append({"id": "p/second", "name": "Second", "reasoning_levels": []})
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
        model = schema["components"]["schemas"]["CatalogModel"]
        self.assertEqual(set(model["required"]), {"id", "name", "reasoning_levels"})

    async def test_reasoning_level_create_validate_and_update(self):
        create = self.main.CreateSessionRequest
        session = await self.main.create_session(create(name="t", project_path=self.tmp.name, agent="pi", model="p/m", reasoning_level="high"))
        self.assertEqual(session["reasoning_level"], "high")
        default = await self.main.create_session(create(name="d", project_path=self.tmp.name, agent="pi", model="p/m"))
        self.assertIsNone(default["reasoning_level"])
        for model, level in [(None, "high"), ("p/m", "max")]:
            with self.assertRaises(HTTPException) as caught:
                await self.main.create_session(create(name="t", project_path=self.tmp.name, agent="pi", model=model, reasoning_level=level))
            self.assertEqual(caught.exception.status_code, 400)

        update = self.main.UpdateSessionRequest
        updated = await self.main.update_session(default["id"], update(reasoning_level="low"))
        self.assertEqual(updated["reasoning_level"], "low")
        with self.assertRaises(HTTPException) as caught:
            await self.main.update_session(default["id"], update(reasoning_level="max", name="unapplied"))
        self.assertEqual(caught.exception.status_code, 400)
        unchanged = await self.main.update_session(default["id"], update(reasoning_level=None))
        self.assertEqual((unchanged["name"], unchanged["reasoning_level"]), ("d", "low"))
        reopened = Database(self.path)
        try:
            self.assertEqual(reopened.get_session(session["id"])["reasoning_level"], "high")
        finally:
            reopened.close()
        self.assertEqual(validate_session_arguments("start_session", {"name": "n", "project_path": "/p", "message": "hi", "model": "p/m", "reasoning_level": "low"})["reasoning_level"], "low")

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
