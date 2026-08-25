import tempfile
import unittest
from pathlib import Path

import tools.skills as skills_module
from tools.skills import create_skill, get_skill, list_skills


class SkillRegistryTests(unittest.TestCase):
    def test_twenty_safe_portable_skills_are_installed(self):
        skills = list_skills(include_instructions=True)
        self.assertGreaterEqual(len(skills), 20)
        self.assertEqual(len({skill['name'] for skill in skills}), len(skills))
        for skill in skills:
            self.assertEqual(skill['command'], f"/{skill['name']}")
            self.assertTrue(skill['description'])
            self.assertTrue(skill['instructions'])
            self.assertIn('instruction-only', skill['safety'])

    def test_skill_lookup_uses_exact_safe_slug(self):
        self.assertEqual(get_skill('web-research')['command'], '/web-research')
        self.assertIsNone(get_skill('../web-research'))

    def test_create_skill_adds_only_a_skill_markdown_file(self):
        original = skills_module.USER_SKILLS_ROOT
        with tempfile.TemporaryDirectory() as directory:
            skills_module.USER_SKILLS_ROOT = Path(directory)
            try:
                created = create_skill('release-check', 'Verify a release before publishing.', 'Run the release checklist.', ['read_file', 'run_shell'])
                self.assertEqual(created['command'], '/release-check')
                path = Path(directory) / 'release-check' / 'SKILL.md'
                self.assertTrue(path.is_file())
                self.assertEqual([item.name for item in path.parent.iterdir()], ['SKILL.md'])
            finally:
                skills_module.USER_SKILLS_ROOT = original
