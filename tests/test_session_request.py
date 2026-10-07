from __future__ import annotations

import unittest

from pydantic import ValidationError

from agent_ui_server.main import CreateSessionRequest


class CreateSessionRequestTests(unittest.TestCase):
    def test_project_path_is_required(self):
        with self.assertRaises(ValidationError):
            CreateSessionRequest(name="test", agent="pi")

    def test_project_path_is_accepted(self):
        request = CreateSessionRequest(name="test", project_path="/project", agent="pi")
        self.assertEqual(request.project_path, "/project")
        self.assertNotIn("working_dir", request.model_dump())


if __name__ == "__main__":
    unittest.main()
