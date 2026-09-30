# ADK標準SkillToolsetとの比較・採否は docs/design/skill_toolset_migration.md を参照（PBI #90）
import os
import yaml
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class SkillRegistry:
    """
    Manages the loading, validation, and retrieval of Agent Skills.
    """
    
    def __init__(self, skills_dirs: List[str]):
        self.skills_dirs = skills_dirs
        self.skills: Dict[str, Dict[str, Any]] = {}
        self.loaded = False

    def load_skills(self):
        """Load all skills from the configured skills directories."""
        for skills_dir in self.skills_dirs:
            if not os.path.exists(skills_dir):
                logger.warning(f"Skills directory not found: {skills_dir}")
                continue

            for root, dirs, files in os.walk(skills_dir):
                # 1. Check for SKILL.md (Standard)
                if "SKILL.md" in files:
                    skill_name = os.path.basename(root)
                    filepath = os.path.join(root, "SKILL.md")
                    try:
                        skill_data = self._load_skill_md(filepath)
                        # Allow frontmatter to override directory name
                        if 'name' not in skill_data:
                            skill_data['name'] = skill_name
                        
                        if self._validate_skill_format(skill_data['name'], skill_data):
                            self.skills[skill_data['name']] = skill_data
                            logger.info(f"Loaded standard skill: {skill_data['name']} from {filepath}")
                    except Exception as e:
                        logger.error(f"Failed to load standard skill from {filepath}: {e}")
                    
                    # If SKILL.md exists, we ignore other files in this directory to avoid duplicates/confusion
                    continue
        
        self.loaded = True

    def _load_skill_md(self, filepath: str) -> Dict[str, Any]:
        """Load a skill from a Markdown file with YAML frontmatter."""
        with open(filepath, 'r') as f:
            content = f.read()

        # Parse Frontmatter
        if content.startswith("---"):
            try:
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    frontmatter = yaml.safe_load(parts[1])
                    instructions = parts[2].strip()
                    
                    if not isinstance(frontmatter, dict):
                        raise ValueError("Invalid frontmatter format")
                        
                    skill_data = frontmatter
                    skill_data['instructions'] = instructions
                    return skill_data
            except Exception as e:
                raise ValueError(f"Failed to parse frontmatter: {e}")
        
        raise ValueError("No valid YAML frontmatter found in SKILL.md")

    def _validate_skill_format(self, name: str, data: Dict) -> bool:
        """Basic format validation for a skill."""
        required_fields = ['name', 'description', 'tools', 'instructions']
        for field in required_fields:
            if field not in data:
                logger.error(f"Skill '{name}' missing required field: {field}")
                return False
        return True

    def validate_skills_against_tools(self, available_tools: List[Any]) -> None:
        """
        Reconcile skills with available MCP tools.
        Removes missing tools from skills. Disables skills if all tools are missing.
        """
        if not self.loaded:
            self.load_skills()
            
        # Create a set of available tool names
        available_tool_names = set()
        for tool in available_tools:
            name = getattr(tool, 'name', str(tool))
            available_tool_names.add(name)
            
        # Also include built-in tools that might be available locally
        # For now, we assume 'run_script' is available if implemented in Agent
        available_tool_names.add('run_script') 

        skills_to_remove = []

        for skill_name, skill_data in self.skills.items():
            required_tools = skill_data.get('tools', [])
            valid_tools = []
            missing_tools = []

            for tool_name in required_tools:
                if tool_name in available_tool_names:
                    valid_tools.append(tool_name)
                else:
                    missing_tools.append(tool_name)
            
            if missing_tools:
                logger.warning(f"Skill '{skill_name}' missing tools: {missing_tools}")
                # Update instructions to reflect missing tools
                if 'instructions' in skill_data:
                    skill_data['instructions'] += f"\n\n(Note: The following tools are currently unavailable: {', '.join(missing_tools)})"

            if not valid_tools:
                logger.warning(f"Skill '{skill_name}' has NO valid tools. Disabling.")
                skills_to_remove.append(skill_name)
            else:
                skill_data['tools'] = valid_tools

        for name in skills_to_remove:
            del self.skills[name]

    def get_skill(self, skill_name: str) -> Optional[Dict[str, Any]]:
        return self.skills.get(skill_name)

    def find_skill_dir(self, skill_name: str) -> Optional[str]:
        """Locate the directory of a skill across all configured skills dirs."""
        for skills_dir in self.skills_dirs:
            candidate = os.path.join(skills_dir, skill_name)
            if os.path.exists(candidate):
                return candidate
        return None

    def list_skills(self) -> List[Dict[str, str]]:
        """Return a list of available skills (metadata only)."""
        return [
            {"name": name, "description": data['description']}
            for name, data in self.skills.items()
        ]
