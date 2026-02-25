    
import base64
import json
import logging
import os
import pickle
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from PIL import Image, ImageDraw
from openai import OpenAI, BadRequestError

from config import (
    EXPLORER_OPENAI_BASE_URL,
    EXPLORER_OPENAI_MODEL_NAME,
    TRAINER_OPENAI_BASE_URL,
    TRAINER_OPENAI_MODEL_NAME,
    TEMPERATURE,
)
from secret_api_keys import API_OPENAI_EXPLORER, API_OPENAI_TRAINER, API_OPENAI_SUMMARIZE
from agent.emulator import Emulator
from agent.memory_reader import PokemonRedReader
from agent.prompts import EXPLORER_SYSTEM_PROMPT_OPENAI, TRAINER_SYSTEM_PROMPT_OPENAI, SUMMARY_PROMPT_OPENAI
from agent.tool_definitions import OPENAI_TOOLS
from agent.simple_agent import LocationCollisionMap, TextDisplay

# Set up logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

MAX_TOKENS_OPENAI = 50000


@dataclass
class AgentContext:
    name: str
    system_prompt: str
    model_name: str
    client: OpenAI
    message_history: list[dict[str, Any]] = field(default_factory=list)
    last_response_time: float = 0.0
    last_tool_explanation: str = ""


class DualAgentCoordinator:
    def __init__(
        self,
        rom_path: str,
        headless: bool = True,
        sound: bool = False,
        max_history: int = 60,
        load_state: Optional[str] = None,
        location_history_length: int = 40,
        location_archive_file_name: Optional[str] = None,
        use_full_collision_map: bool = True,
        pyboy_main_thread: bool = False,
        rag_path: str = "rag/memory_summary.json",
    ):
        self.emulator = Emulator()
        self.pyboy_main_thread = pyboy_main_thread
        self.emulator_init_kwargs = {
            "rom_path": rom_path,
            "headless": headless,
            "sound": sound,
            "pyboy_main_thread": self.pyboy_main_thread,
        }
        if not self.pyboy_main_thread:
            self.emulator.initialize(**self.emulator_init_kwargs)

        explorer_client = OpenAI(
            api_key=API_OPENAI_EXPLORER,
            base_url=EXPLORER_OPENAI_BASE_URL or None,
        )
        trainer_client = OpenAI(
            api_key=API_OPENAI_TRAINER,
            base_url=TRAINER_OPENAI_BASE_URL or None,
        )

        # Client dedicato per il summarize (memoria RAG)
        from config import SUMMARIZE_OPENAI_MODEL_NAME, SUMMARIZE_OPENAI_BASE_URL
        from secret_api_keys import API_OPENAI_SUMMARIZE
        self.summarize_client = OpenAI(
            api_key=API_OPENAI_SUMMARIZE,
            base_url=SUMMARIZE_OPENAI_BASE_URL or None,
        )

        self.explorer = AgentContext(
            name="explorer",
            system_prompt=EXPLORER_SYSTEM_PROMPT_OPENAI,
            model_name=EXPLORER_OPENAI_MODEL_NAME,
            client=explorer_client,
            message_history=[{"role": "user", "content": "You may now begin playing."}],
        )
        self.trainer = AgentContext(
            name="trainer",
            system_prompt=TRAINER_SYSTEM_PROMPT_OPENAI,
            model_name=TRAINER_OPENAI_MODEL_NAME,
            client=trainer_client,
            message_history=[{"role": "user", "content": "You may now begin playing."}],
        )

        self.running = True
        self.max_history = max_history
        self.location_history_length = location_history_length
        self.location_archive_file_name = location_archive_file_name
        self.location_history: list[tuple[str, tuple[int, int]]] = []
        self.label_archive: dict[str, dict[int, dict[int, str]]] = {}
        self.location_tracker_activated: bool = False
        self.location_tracker: dict[str, list[list[bool]]] = {}
        self.steps_since_checkpoint = 0
        self.steps_since_label_reset = 0
        self.last_location: Optional[str] = None
        self.map_tool_map: dict[str, str] = {}
        self.full_collision_map: dict[str, LocationCollisionMap] = {}
        self.use_full_collision_map = use_full_collision_map
        self.absolute_step_count = 0
        self.all_visited_locations: set[str] = set()
        self.location_milestones: list[tuple[str, int]] = []
        self.text_display = TextDisplay()
        self.last_coords = None
        self.checkpoints: list[str] = []
        self.load_state = load_state
        self.rag_path = rag_path
        self.objective_explorer: str = ""
        self.objective_trainer: str = ""
        self.pending_opinions: dict[str, list[str]] = {"explorer": [], "trainer": []}
        self.ui_state_path = os.path.join("ui", "state.json")
        self.ui_message_log: dict[str, list[str]] = {"explorer": [], "trainer": []}
        self.ui_tool_log: dict[str, list[dict[str, Any]]] = {"explorer": [], "trainer": []}
        self.ui_token_log: dict[str, list[dict[str, Any]]] = {"explorer": [], "trainer": []}
        self.ui_opinion_log: dict[str, list[str]] = {"explorer": [], "trainer": []}
        self.last_summary_time = 0.0
        self.ui_max_messages = 200
        self.ui_max_tools = 50
        self.ui_max_tokens = 80
        self.ui_max_opinions = 50

        if load_state and not self.pyboy_main_thread:
            logger.info(f"Loading saved state from {load_state}")
            self.emulator.load_state(load_state)
            self.load_location_archive(location_archive_file_name)
        elif load_state:
            self.load_location_archive(location_archive_file_name)

        self._load_objectives_from_rag()
        self._write_ui_state()

    def _wrap_openai_tools_for_chat(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        wrapped = []
        for tool in tools:
            wrapped.append({
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {}),
                },
            })
        return wrapped

    def _get_rag_text(self) -> str:
        if not os.path.exists(self.rag_path):
            return ""
        try:
            with open(self.rag_path, "r", encoding="utf-8") as fr:
                return fr.read().strip()
        except Exception:
            return ""

    def _get_rag_messages_text(self) -> str:
        """Restituisce solo il contenuto di 'messages' (summary precedente), non le note.
        Così il summarizer non vede le note e non le ripete nell'output."""
        if not os.path.exists(self.rag_path):
            return ""
        try:
            with open(self.rag_path, "r", encoding="utf-8") as fr:
                data = json.load(fr)
            if not isinstance(data, dict):
                return ""
            messages = data.get("messages", [])
            if isinstance(messages, list):
                return "\n\n".join(str(m) for m in messages)
            return str(messages)
        except Exception:
            return ""

    def _write_rag_text(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.rag_path), exist_ok=True)
        # Se il testo è un JSON valido con chiavi 'notes' e 'messages', salva tutto. Altrimenti aggiorna solo 'messages'.
        try:
            new_data = json.loads(text)
            if isinstance(new_data, dict) and "messages" in new_data:
                # Preserva objectives esistenti quando il summarizer scrive (evita perdita tra interruzioni)
                if os.path.exists(self.rag_path):
                    with open(self.rag_path, "r", encoding="utf-8") as fr:
                        try:
                            current = json.load(fr)
                            if isinstance(current, dict) and "objectives" in current:
                                new_data["objectives"] = current["objectives"]
                        except Exception:
                            pass
                if "objectives" not in new_data:
                    new_data["objectives"] = {
                        "explorer": self.objective_explorer or "",
                        "trainer": self.objective_trainer or "",
                    }
                with open(self.rag_path, "w", encoding="utf-8") as fw:
                    json.dump(new_data, fw, ensure_ascii=False, indent=2)
                return
        except Exception:
            pass
        # Altrimenti aggiorna solo 'messages', lasciando intatte le note
        if os.path.exists(self.rag_path):
            with open(self.rag_path, "r", encoding="utf-8") as fr:
                try:
                    data = json.load(fr)
                    if not isinstance(data, dict):
                        data = {"notes": [], "messages": []}
                except Exception:
                    data = {"notes": [], "messages": []}
        else:
            data = {"notes": [], "messages": []}
        data["messages"] = text if isinstance(text, list) else [text]
        with open(self.rag_path, "w", encoding="utf-8") as fw:
            json.dump(data, fw, ensure_ascii=False, indent=2)

    def _load_objectives_from_rag(self) -> None:
        """Carica gli obiettivi explorer/trainer dal RAG se presenti (persistenza tra interruzioni)."""
        if not os.path.exists(self.rag_path):
            return
        try:
            with open(self.rag_path, "r", encoding="utf-8") as fr:
                data = json.load(fr)
            if not isinstance(data, dict):
                return
            objs = data.get("objectives")
            if isinstance(objs, dict):
                if "explorer" in objs and isinstance(objs["explorer"], str):
                    self.objective_explorer = objs["explorer"]
                if "trainer" in objs and isinstance(objs["trainer"], str):
                    self.objective_trainer = objs["trainer"]
        except Exception as e:
            logger.debug("Could not load objectives from RAG: %s", e)

    def _save_objectives_to_rag(self) -> None:
        """Salva gli obiettivi corrente nel RAG (persistenza tra interruzioni)."""
        os.makedirs(os.path.dirname(self.rag_path), exist_ok=True)
        if os.path.exists(self.rag_path):
            with open(self.rag_path, "r", encoding="utf-8") as fr:
                try:
                    data = json.load(fr)
                    if not isinstance(data, dict):
                        data = {"notes": [], "messages": []}
                except Exception:
                    data = {"notes": [], "messages": []}
        else:
            data = {"notes": [], "messages": []}
        data["objectives"] = {
            "explorer": self.objective_explorer or "",
            "trainer": self.objective_trainer or "",
        }
        with open(self.rag_path, "w", encoding="utf-8") as fw:
            json.dump(data, fw, ensure_ascii=False, indent=2)

    def append_message_to_rag(self, message_entry: dict) -> None:
        """Aggiunge un messaggio a 'messages' in memory_summary.json senza toccare le note."""
        notes_path = self.rag_path
        if os.path.exists(notes_path):
            with open(notes_path, "r", encoding="utf-8") as fr:
                try:
                    data = json.load(fr)
                    if not isinstance(data, dict):
                        data = {"notes": [], "messages": []}
                except Exception:
                    data = {"notes": [], "messages": []}
        else:
            data = {"notes": [], "messages": []}
        messages = data.get("messages", [])
        messages.append(message_entry)
        data["messages"] = messages
        with open(notes_path, "w", encoding="utf-8") as fw:
            json.dump(data, fw, ensure_ascii=False, indent=2)

    def _append_ui_message(self, agent_name: str, text: str) -> None:
        if not text:
            return
        log = self.ui_message_log.setdefault(agent_name, [])
        log.append(text)
        if len(log) > self.ui_max_messages:
            self.ui_message_log[agent_name] = log[-self.ui_max_messages :]

    def _append_ui_tool_call(self, agent_name: str, tool_name: str, arguments: str) -> None:
        log = self.ui_tool_log.setdefault(agent_name, [])
        log.append({"name": tool_name, "arguments": arguments})
        if len(log) > self.ui_max_tools:
            self.ui_tool_log[agent_name] = log[-self.ui_max_tools :]

    def _append_ui_opinion(self, agent_name: str, opinion: str) -> None:
        print(f"[DEBUG] _append_ui_opinion called: agent_name={agent_name}, opinion={opinion}")
        if not opinion:
            return
        log = self.ui_opinion_log.setdefault(agent_name, [])
        log.append(opinion)
        if len(log) > self.ui_max_opinions:
            self.ui_opinion_log[agent_name] = log[-self.ui_max_opinions :]

    def _append_ui_token_usage(self, agent_name: str, response: Any, model_name: str) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if prompt_tokens is None and completion_tokens is None and total_tokens is None:
            return
        log = self.ui_token_log.setdefault(agent_name, [])
        log.append({
            "timestamp": time.time(),
            "model": getattr(response, "model", model_name),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        })
        if len(log) > self.ui_max_tokens:
            self.ui_token_log[agent_name] = log[-self.ui_max_tokens :]

    def _write_ui_state(self) -> None:
        os.makedirs(os.path.dirname(self.ui_state_path), exist_ok=True)
        state = {
            "timestamp": time.time(),
            "last_summary_time": self.last_summary_time,
            "objective": {
                "explorer": self.objective_explorer,
                "trainer": self.objective_trainer
            },
            "agents": {
                "explorer": {
                    "messages": self.ui_message_log.get("explorer", []),
                    "tools": self.ui_tool_log.get("explorer", []),
                    "tokens": self.ui_token_log.get("explorer", []),
                },
                "trainer": {
                    "messages": self.ui_message_log.get("trainer", []),
                    "tools": self.ui_tool_log.get("trainer", []),
                    "tokens": self.ui_token_log.get("trainer", []),
                },
            },
            "opinions": {
                "explorer": self.ui_opinion_log.get("explorer", []),
                "trainer": self.ui_opinion_log.get("trainer", []),
            },
        }
        with open(self.ui_state_path, "w", encoding="utf-8") as fw:
            json.dump(state, fw)

    def _summarize_to_rag(self) -> None:
        memory_info, location, coords = self.emulator.get_state_from_memory()
        last_checkpoints = "\n".join(self.checkpoints[-10:])
        explorer_history = self._render_history_text(self.explorer, limit=10)
        trainer_history = self._render_history_text(self.trainer, limit=10)
        rag_messages = self._get_rag_messages_text()

        prompt = (
            "Update the memory by merging PREVIOUS_MEMORY with NEW_INPUT. "
            "Keep stable facts from PREVIOUS_MEMORY unless NEW_INPUT clearly updates them.\n\n"
            f"Location: {location} at {coords}\n"
            f"Explorer objective: {self.objective_explorer or 'None'}\n"
            f"Trainer objective: {self.objective_trainer or 'None'}\n"
            f"Last checkpoints:\n{last_checkpoints}\n\n"
            f"Explorer recent history:\n{explorer_history}\n\n"
            f"Trainer recent history:\n{trainer_history}\n\n"
            f"Previous summary (your past output, do not duplicate notes):\n{rag_messages}"
        )

        try:
            from config import SUMMARIZE_OPENAI_MODEL_NAME
            from agent.prompts import SUMMARY_PROMPT_OPENAI
            response = self.summarize_client.chat.completions.create(
                model=SUMMARIZE_OPENAI_MODEL_NAME,
                messages=[
                    {"role": "system", "content": SUMMARY_PROMPT_OPENAI},
                    {"role": "user", "content": prompt},
                ]
            )
            summary_text = response.choices[0].message.content or ""
            if summary_text:
                self._write_rag_text(summary_text)
                self.text_display.add_message("[RAG] Memory summary updated")
                self.last_summary_time = time.time()
                self._write_ui_state()
        except Exception as exc:
            logger.error(f"Failed to update RAG summary: {exc}")

    def _render_history_text(self, agent: AgentContext, limit: int = 10) -> str:
        lines: list[str] = []
        for message in agent.message_history[-limit:]:
            role = message.get("role", "")
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for entry in content:
                    if isinstance(entry, dict) and entry.get("type") == "text":
                        text_parts.append(entry.get("text", ""))
                content = " ".join(text_parts)
            if not isinstance(content, str):
                continue
            if content.strip():
                lines.append(f"{role}: {content.strip()}")
        return "\n".join(lines)

    def load_location_archive(self, filename: Optional[str]) -> None:
        if not filename:
            return
        if not os.path.exists(filename):
            return
        try:
            with open(filename, "rb") as fr:
                self.label_archive = pickle.load(fr)
        except Exception:
            pass

    def save_location_archive(self, filename: Optional[str]) -> None:
        if not filename:
            return
        try:
            with open(filename, "wb") as fw:
                pickle.dump(self.label_archive, fw)
        except Exception:
            pass

    def update_and_get_full_collision_map(self, location: str, coords: tuple[int, int]) -> str:
        collision_map = self.emulator.pyboy.game_wrapper.game_area_collision()
        downsampled_terrain = self.emulator._downsample_array(collision_map)
        local_location_tracker = self.location_tracker.get(location, [])
        this_map = self.full_collision_map.get(location)
        if this_map is None:
            self.full_collision_map[location] = LocationCollisionMap(
                downsampled_terrain, self.emulator.get_sprites(), coords
            )
            return self.full_collision_map[location].to_ascii(local_location_tracker)
        this_map.update_map(downsampled_terrain, self.emulator.get_sprites(), coords)
        return this_map.to_ascii(local_location_tracker)

    def get_all_location_labels(self, location: str) -> list[tuple[tuple[int, int], str]]:
        all_labels: list[tuple[tuple[int, int], str]] = []
        this_location = self.label_archive.get(location)
        if this_location is None:
            for key, value in self.label_archive.items():
                if location.lower() == key.lower():
                    this_location = value
                    break
        if this_location:
            max_row = max(this_location.keys())
            for nearby_row in range(max_row + 1):
                this_row = this_location.get(nearby_row)
                if this_row is None:
                    continue
                max_col = max(this_row.keys())
                for nearby_col in range(max_col + 1):
                    this_col = this_row.get(nearby_col)
                    if this_col is not None:
                        all_labels.append(((nearby_col, nearby_row), this_col))
        return all_labels

    def _get_ram_info_explorer(self) -> tuple[str, str, tuple[int, int]]:
        reader = PokemonRedReader(self.emulator.pyboy.memory)
        location = reader.read_location()
        coords = reader.read_coordinates()
        dialog = reader.read_dialog() or "None"
        name = reader.read_player_name()
        rival_name = reader.read_rival_name()
        if name == "NINTEN":
            name = "Not yet set"
        if rival_name == "SONY":
            rival_name = "Not yet set"
        valid_moves = ", ".join(self.emulator.get_valid_moves()) or "None"
        facing = self.emulator.get_facing_direction()
        badges = ", ".join(reader.read_badges()) or "None"
        ram_text = (
            f"Player: {name}\n"
            f"Rival: {rival_name}\n"
            f"RAM Location: {location}\n"
            f"Coordinates (col,row): {coords}\n"
            f"Facing: {facing}\n"
            f"Valid Moves: {valid_moves}\n"
            f"Badges: {badges}\n"
            f"Dialog: {dialog}\n"
        )
        return ram_text, location, coords

    def get_screenshot_base64(
        self,
        screenshot: Image.Image,
        upscale: int = 1,
        add_coords: bool = True,
        player_coords: Optional[tuple[int, int]] = None,
        location: Optional[str] = None,
        relative_square_size: int = 8,
    ) -> str:
        if upscale > 1:
            new_size = (screenshot.width * upscale, screenshot.height * upscale)
            screenshot = screenshot.resize(new_size)

        past_locations = self.location_history
        location_labels = self.label_archive.get(location or "")
        if location_labels is None:
            for key, value in self.label_archive.items():
                if location and location.lower() == key.lower():
                    location_labels = value
                    break
        if location_labels is None:
            location_labels = {}
        local_location_tracker = self.location_tracker.get(location or "", [])

        collision_map = self.emulator.pyboy.game_wrapper.game_area_collision()
        downsampled_terrain = self.emulator._downsample_array(collision_map)
        sprite_locations = self.emulator.get_sprites()

        if not self.emulator.get_in_combat():
            shape = screenshot.size
            for x in range(0, shape[0], shape[0] // 10):
                draw = ImageDraw.Draw(screenshot)
                draw.line((x, 0, x, shape[1]), fill=(0, 0, 0), width=1)
            for y in range(0, shape[1], shape[1] // 9):
                draw = ImageDraw.Draw(screenshot)
                draw.line((0, y, shape[0], y), fill=(0, 0, 0), width=1)

        if add_coords and player_coords and not self.emulator.get_in_combat():
            draw = ImageDraw.Draw(screenshot)
            for row in range(9):
                for col in range(10):
                    x = col * screenshot.width // 10
                    y = row * screenshot.height // 9
                    col_coord = player_coords[0] + col - 4
                    row_coord = player_coords[1] + row - 4
                    label = f"{col_coord},{row_coord}"
                    draw.text((x + 2, y + 2), label, fill=(0, 0, 0))

        for row in range(9):
            for col in range(10):
                if (col, row) in sprite_locations:
                    x0 = col * screenshot.width // 10
                    y0 = row * screenshot.height // 9
                    x1 = (col + 1) * screenshot.width // 10
                    y1 = (row + 1) * screenshot.height // 9
                    draw = ImageDraw.Draw(screenshot)
                    draw.rectangle([x0, y0, x1, y1], outline=(255, 255, 0), width=2)
                elif downsampled_terrain[row][col] == 0:
                    x0 = col * screenshot.width // 10
                    y0 = row * screenshot.height // 9
                    x1 = (col + 1) * screenshot.width // 10
                    y1 = (row + 1) * screenshot.height // 9
                    draw = ImageDraw.Draw(screenshot)
                    draw.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=2)

        if location_labels:
            for row_ind, this_row in location_labels.items():
                for col_ind, label in this_row.items():
                    if player_coords is None:
                        continue
                    local_col = col_ind - player_coords[0] + 4
                    local_row = row_ind - player_coords[1] + 4
                    if local_col < 0 or local_row < 0 or local_col > 9 or local_row > 8:
                        continue
                    x = local_col * screenshot.width // 10
                    y = local_row * screenshot.height // 9
                    draw = ImageDraw.Draw(screenshot)
                    draw.text((x + 2, y + 2), label, fill=(0, 0, 0))

        if local_location_tracker and player_coords:
            for col in range(len(local_location_tracker)):
                for row in range(len(local_location_tracker[col])):
                    if not local_location_tracker[col][row]:
                        continue
                    local_col = col - player_coords[0] + 4
                    local_row = row - player_coords[1] + 4
                    if local_col < 0 or local_row < 0 or local_col > 9 or local_row > 8:
                        continue
                    x0 = local_col * screenshot.width // 10
                    y0 = local_row * screenshot.height // 9
                    x1 = (local_col + 1) * screenshot.width // 10
                    y1 = (local_row + 1) * screenshot.height // 9
                    draw = ImageDraw.Draw(screenshot)
                    draw.rectangle([x0, y0, x1, y1], outline=(0, 255, 255), width=2)

        buffered = base64.b64encode(self._image_to_bytes(screenshot)).decode("utf-8")
        return buffered

    @staticmethod
    def _image_to_bytes(image: Image.Image) -> bytes:
        from io import BytesIO

        with BytesIO() as output_bytes:
            image.save(output_bytes, format="PNG")
            return output_bytes.getvalue()

    def _build_snapshot_message(self, agent: AgentContext) -> dict[str, Any]:
        rag_text = self._get_rag_text()
        if agent.name == "explorer":
            ram_text, location, coords = self._get_ram_info_explorer()
        else:
            ram_text, location, coords = self.emulator.get_state_from_memory()

        other_agent = self.trainer if agent.name == "explorer" else self.explorer
        other_last_explanation = other_agent.last_tool_explanation or "None"

        all_labels = self.get_all_location_labels(location)
        label_text = ", ".join(f"{coords}: {label}" for coords, label in all_labels) or "None"
        last_checkpoints = "\n".join(self.checkpoints[-10:])
        opinions = self.pending_opinions.get(agent.name, [])

        map_text = ""
        # Include the full collision map when available for any agent (explorer or trainer)
        if not self.emulator.get_in_combat() and self.use_full_collision_map:
            map_text = self.update_and_get_full_collision_map(location, coords)

        screenshot = self.emulator.get_screenshot()
        screenshot_b64 = self.get_screenshot_base64(
            screenshot,
            upscale=4,
            add_coords=True,
            player_coords=coords,
            location=location,
        )

        text_sections = [
            f"Agent: {agent.name}",
            f"Explorer objective: {self.objective_explorer or 'None'}",
            f"Trainer objective: {self.objective_trainer or 'None'}",
            f"Opinions for you: {('; '.join(opinions)) if opinions else 'None'}",
            f"Last tool explanation from other agent: {other_last_explanation}",
            f"RAM snapshot:\n{ram_text}",
            f"Labeled nearby locations: {label_text}",
            f"Last checkpoints:\n{last_checkpoints or 'None'}",
        ]
        if map_text:
            text_sections.append(f"TEXT_MAP:\n{map_text}")
        if rag_text:
            text_sections.append(f"RAG_MEMORY_JSON:\n{rag_text}")

        self.pending_opinions[agent.name] = []

        return {
            "role": "user",
            "content": [
                {"type": "text", "text": "\n\n".join(text_sections)},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                },
            ],
        }

    @staticmethod
    def _get_last_assistant_message(agent: AgentContext) -> str:
        for message in reversed(agent.message_history):
            if message.get("role") != "assistant":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for entry in content:
                    if isinstance(entry, dict) and entry.get("type") == "text":
                        text_parts.append(entry.get("text", ""))
                content = " ".join(text_parts)
            if isinstance(content, str) and content.strip():
                return content.strip()
        return ""

    def _build_tool_result(self, tool_use_id: str, text: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [
                {"type": "text", "text": text}
            ],
        }

    def press_buttons(self, buttons: list[str], wait: bool) -> tuple[str, tuple[int, int]]:
        self.text_display.add_message(f"[Buttons] Pressing: {buttons} (wait={wait})")
        result, last_coords = self.emulator.press_buttons(buttons, wait)
        self.last_coords = last_coords

        memory_info, location, coords = self.emulator.get_state_from_memory()
        logger.info("[Memory State after action]")
        logger.info(memory_info)

        self._record_position(location, coords)

        return result, last_coords

    def navigate_to(self, row: int, col: int) -> str:
        memory_info, location, coords = self.emulator.get_state_from_memory()
        self.text_display.add_message(f"[Navigation] Navigating to: ({col}, {row})")

        local_col = col - coords[0] + 4
        local_row = row - coords[1] + 4

        status, path = self.emulator.find_path(local_row, local_col)
        last_coords = coords
        next_coords = coords
        if path:
            for direction in path:
                self.emulator.press_buttons([direction], True, wait_for_finish=False)
                cur_coords = self.emulator.get_coordinates()
                if cur_coords != next_coords:
                    last_coords = next_coords
                    next_coords = cur_coords
            self.last_coords = last_coords
            _, new_location, new_coords = self.emulator.get_state_from_memory()
            self._record_position(new_location, new_coords)
            return f"Navigation successful: followed path with {len(path)} steps"
        return f"Navigation failed: {status}"

    def navigate_to_offscreen_coordinate(self, row: int, col: int) -> str:
        memory_info, location, coords = self.emulator.get_state_from_memory()
        self.update_and_get_full_collision_map(location, coords)
        final_distance = self.full_collision_map[location].distances.get((col, row))
        if final_distance is None:
            return "Invalid coordinates; navigation too far or not possible."
        buttons = self.full_collision_map[location].generate_buttons_to_coord(col, row)
        if not buttons:
            return "No path available to the target coordinate."
        self.press_buttons(buttons, True)
        return f"Navigation successful: followed path with {len(buttons)} steps"

    def _record_position(self, location: str, coords: tuple[int, int]) -> None:
        self.location_history.insert(0, (location, coords))
        if len(self.location_history) > self.location_history_length:
            self.location_history.pop()

        if self.location_tracker_activated:
            cols = self.location_tracker.setdefault(location, [])
            if coords[0] > len(cols) - 1:
                if len(cols) == 0:
                    cols.extend(list() for _ in range(0, coords[0] + 1))
                else:
                    cols.extend([False for _ in range(0, len(cols[0]))] for _ in range(0, coords[0] + 1))
            if coords[1] > len(cols[0]) - 1:
                for col in cols:
                    col.extend(False for _ in range(0, coords[1] + 1))
            cols[coords[0]][coords[1]] = True

    def process_tool_call(self, agent: AgentContext, tool_call: Any) -> dict[str, Any]:
        tool_name = tool_call.function.name
        tool_input = json.loads(tool_call.function.arguments)
        tool_id = tool_call.id
        explanation = tool_input.get("explanation_of_action", "")
        if explanation:
            self.text_display.add_message(f"[Text] {explanation}")
            agent.last_tool_explanation = explanation
        logger.info(f"Processing tool call: {tool_name}")

        if tool_name == "press_buttons":
            buttons = tool_input["buttons"]
            wait = tool_input.get("wait", True)
            result, _ = self.press_buttons(buttons, wait)
            return self._build_tool_result(tool_id, f"Pressed buttons: {', '.join(buttons)}. {result}")
        if tool_name == "navigate_to":
            row = tool_input["row"]
            col = tool_input["col"]
            result = self.navigate_to(row, col)
            return self._build_tool_result(tool_id, result)
        if tool_name == "navigate_to_offscreen_coordinate":
            row = tool_input["row"]
            col = tool_input["col"]
            result = self.navigate_to_offscreen_coordinate(row, col)
            return self._build_tool_result(tool_id, result)
        if tool_name == "bookmark_location_or_overwrite_label":
            location = tool_input["location"]
            row = tool_input["row"]
            col = tool_input["col"]
            label = tool_input["label"]
            self.text_display.add_message(f"Logging {location}, ({col}, {row}) as {label}")
            self.label_archive.setdefault(location.lower(), {}).setdefault(row, {})[col] = label
            return self._build_tool_result(tool_id, f"Location labeled: {location}, ({col}, {row}) as {label}")
        if tool_name == "mark_checkpoint":
            self.steps_since_checkpoint = 0
            self.steps_since_label_reset = 0
            self.location_tracker_activated = False
            achievement = tool_input.get("achievement", "")
            if achievement:
                self.checkpoints.append(achievement)
                self.text_display.add_message(f"Checkpoint marked: {achievement}")
            return self._build_tool_result(tool_id, "Checkpoint set.")
        if tool_name == "detailed_navigator":
            return self._build_tool_result(tool_id, "Navigator mode is not enabled in dual-agent mode.")
        if tool_name == "opinion":
            # Error handling for missing required fields
            missing_fields = []
            target = tool_input.get("to")
            opinion = tool_input.get("opinion")
            if target is None:
                missing_fields.append("to")
            if opinion is None:
                missing_fields.append("opinion")
            if missing_fields:
                logger.error(f"Malformed 'opinion' tool call: missing fields {missing_fields}. Input: {tool_input}")
                return self._build_tool_result(tool_id, f"Error: Malformed 'opinion' tool call. Missing fields: {', '.join(missing_fields)}.")
            if target in self.pending_opinions:
                self.pending_opinions[target].append(opinion)
                self._append_ui_opinion(target, opinion)
            return self._build_tool_result(tool_id, f"Opinion sent to {target}.")

        if tool_name == "objective":
            # Support both string and dict objective assignment
            # If called by explorer, set explorer objective; if by trainer, set trainer objective
            # Also support shared update if both keys present
            if "explorer" in tool_input or "trainer" in tool_input:
                if "explorer" in tool_input:
                    self.objective_explorer = tool_input["explorer"]
                if "trainer" in tool_input:
                    self.objective_trainer = tool_input["trainer"]
            elif "objective" in tool_input:
                # If only 'objective' key, assign to the calling agent
                if agent.name == "explorer":
                    self.objective_explorer = tool_input["objective"]
                elif agent.name == "trainer":
                    self.objective_trainer = tool_input["objective"]
            self._save_objectives_to_rag()
            self._write_ui_state()
            return self._build_tool_result(tool_id, "Objectives updated.")

        if tool_name == "remember_note":
            note_text = tool_input["text"][:300]
            tags = tool_input.get("tags", [])
            note_entry = {
                "timestamp": time.time(),
                "text": note_text,
                "tags": tags,
                "agent": agent.name,
            }
            # Save as a list of notes in rag/memory_summary.json
            notes_path = self.rag_path
            try:
                if os.path.exists(notes_path):
                    with open(notes_path, "r", encoding="utf-8") as fr:
                        try:
                            data = json.load(fr)
                            if isinstance(data, dict):
                                notes = data.get("notes", [])
                            elif isinstance(data, list):
                                # Legacy format: bare list of notes
                                notes = data
                                data = {"notes": notes, "messages": []}
                            else:
                                notes = []
                                data = {"notes": [], "messages": []}
                        except Exception:
                            notes = []
                            data = {"notes": [], "messages": []}
                else:
                    notes = []
                    data = {"notes": [], "messages": []}
                # Avoid duplicates (same text, agent and tags)
                if not any(
                    n.get("text") == note_text
                    and n.get("agent") == agent.name
                    and n.get("tags", []) == tags
                    for n in notes
                ):
                    notes.append(note_entry)
                    data["notes"] = notes
                    with open(notes_path, "w", encoding="utf-8") as fw:
                        json.dump(data, fw, ensure_ascii=False, indent=2)
                    self.text_display.add_message(
                        f"[RAG] Note added: {note_text[:60]}{'...' if len(note_text) > 60 else ''}"
                    )
                    return self._build_tool_result(tool_id, "Note added to RAG memory.")
                else:
                    return self._build_tool_result(tool_id, "Note already present in RAG memory.")
            except Exception as e:
                logger.error(f"Error while saving RAG note: {e}")
                return self._build_tool_result(tool_id, f"Error while saving note: {e}")

        if tool_name == "delete_remember_note":
            notes_path = self.rag_path
            try:
                if not os.path.exists(notes_path):
                    return self._build_tool_result(tool_id, "No RAG memory file found.")

                # Require explicit confirmation flag before deleting
                if not tool_input.get("confirm", False):
                    return self._build_tool_result(
                        tool_id,
                        "Deletion not performed: set 'confirm' to true to delete notes from RAG memory.",
                    )

                with open(notes_path, "r", encoding="utf-8") as fr:
                    try:
                        data = json.load(fr)
                        if isinstance(data, dict):
                            notes = data.get("notes", [])
                        elif isinstance(data, list):
                            # Legacy format: bare list of notes
                            notes = data
                            data = {"notes": notes, "messages": []}
                        else:
                            notes = []
                            data = {"notes": [], "messages": []}
                    except Exception:
                        return self._build_tool_result(tool_id, "Error while reading RAG file.")

                orig_notes = list(notes)
                timestamp = tool_input.get("timestamp", None)
                text = tool_input.get("text", None)
                if timestamp is None and text is None:
                    return self._build_tool_result(
                        tool_id,
                        "Error: provide either 'timestamp' or 'text' to identify the note to delete.",
                    )

                removed = []
                # Delete by timestamp (with small tolerance)
                if timestamp is not None:
                    tol = 1e-3
                    remaining = []
                    for n in orig_notes:
                        try:
                            n_ts = float(n.get("timestamp", 0))
                        except Exception:
                            n_ts = 0
                        if abs(n_ts - float(timestamp)) <= tol:
                            removed.append(n)
                        else:
                            remaining.append(n)
                    notes = remaining
                else:
                    # Delete by exact text match (may remove multiple entries)
                    removed = [n for n in orig_notes if n.get("text") == text]
                    notes = [n for n in orig_notes if n.get("text") != text]

                data["notes"] = notes
                with open(notes_path, "w", encoding="utf-8") as fw:
                    json.dump(data, fw, ensure_ascii=False, indent=2)

                if removed:
                    self.text_display.add_message(f"[RAG] Notes removed: {len(removed)}")
                    return self._build_tool_result(
                        tool_id, f"Removed {len(removed)} note(s) from RAG memory."
                    )
                else:
                    return self._build_tool_result(
                        tool_id, "No matching notes found to delete."
                    )
            except Exception as e:
                logger.error(f"Error while deleting RAG note: {e}")
                return self._build_tool_result(tool_id, f"Error while deleting note: {e}")

        return self._build_tool_result(tool_id, f"Error: Unknown tool '{tool_name}'")

    def _call_agent(self, agent: AgentContext, user_message: dict[str, Any]) -> tuple[AgentContext, Any]:
        agent.message_history.append(user_message)
        retries = 2
        cur_tries = 0
        while cur_tries < retries:
            try:
                base = str(getattr(agent.client, "base_url", None) or "http://localhost:8080").rstrip("/")
                logger.info(f"HTTP Request: POST {base}/api/v1/chat/completions [agent={agent.name}]")
                response = agent.client.chat.completions.create(
                    model=agent.model_name,
                    messages=[{"role": "system", "content": agent.system_prompt}] + agent.message_history,
                    temperature=TEMPERATURE,
                    max_completion_tokens=MAX_TOKENS_OPENAI,
                    tools=self._wrap_openai_tools_for_chat(OPENAI_TOOLS),
                )
                agent.last_response_time = time.time()
                return agent, response
            except BadRequestError as e:
                logger.error(f"[agent={agent.name}] BadRequestError: {e}")
                if hasattr(e, 'response') and hasattr(e.response, 'text'):
                    logger.error(f"Response content: {e.response.text}")
                cur_tries += 1
                continue
            except Exception as e:
                logger.error(f"[agent={agent.name}] Unexpected error during OpenAI call: {e}")
                cur_tries += 1
                continue
        agent.last_response_time = time.time()
        return agent, None

    def _handle_agent_response(self, agent: AgentContext, response: Any) -> None:
        if response is None:
            return
        message = response.choices[0].message
        response_texts = message.content or ""
        self._append_ui_token_usage(agent.name, response, agent.model_name)
        if response_texts:
            self.text_display.add_message(f"[{agent.name}] {response_texts}")
            self._append_ui_message(agent.name, response_texts)
        assistant_message: dict[str, Any] = {"role": "assistant", "content": response_texts}
        if message.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        agent.message_history.append(assistant_message)

        if not message.tool_calls:
            agent.message_history.append({
                "role": "user",
                "content": [{"type": "text", "text": "Please continue playing."}],
            })
            self._write_ui_state()
            return

        for call in message.tool_calls:
            self._append_ui_tool_call(agent.name, call.function.name, call.function.arguments)
            tool_result = self.process_tool_call(agent, call)
            agent.message_history.append({
                "role": "tool",
                "tool_call_id": tool_result["tool_use_id"],
                "content": json.dumps(tool_result["content"]),
            })

        self._write_ui_state()

    def _maybe_truncate_histories(self) -> None:
        if len(self.explorer.message_history) < self.max_history and len(self.trainer.message_history) < self.max_history:
            return
        self._summarize_to_rag()
        keep_last = 10
        self.explorer.message_history = self.explorer.message_history[-keep_last:]
        self.trainer.message_history = self.trainer.message_history[-keep_last:]

    def run(self, num_steps: int = 1, save_every: int = 10, save_file_name: Optional[str] = None) -> int:
        logger.info(f"Starting dual-agent loop for {num_steps} steps")

        if self.pyboy_main_thread:
            self.emulator.wait_for_pyboy()
            if self.load_state:
                logger.info(f"Loading saved state from {self.load_state}")
                self.emulator.load_state(self.load_state)

        steps_completed = 0
        while self.running and steps_completed < num_steps:
            try:
                location = self.emulator.get_location()
                coords = self.emulator.get_coordinates()
                if location not in self.all_visited_locations:
                    self.text_display.add_message(f"New Location reached! {location} at {self.absolute_step_count}")
                    self.location_milestones.append((location, self.absolute_step_count))
                    self.all_visited_locations.add(location)
                self.last_coords = coords

                responses: list[tuple[AgentContext, Any]] = []
                threads: list[threading.Thread] = []

                explorer_message = self._build_snapshot_message(self.explorer)
                trainer_message = self._build_snapshot_message(self.trainer)

                def call_agent(agent_ctx: AgentContext, user_message: dict[str, Any]) -> None:
                    responses.append(self._call_agent(agent_ctx, user_message))

                for agent_ctx, user_message in (
                    (self.explorer, explorer_message),
                    (self.trainer, trainer_message),
                ):
                    thread = threading.Thread(target=call_agent, args=(agent_ctx, user_message))
                    threads.append(thread)
                    thread.start()

                for thread in threads:
                    thread.join()

                responses.sort(key=lambda x: x[0].last_response_time)
                for agent_ctx, response in responses:
                    self._handle_agent_response(agent_ctx, response)

                self._maybe_truncate_histories()

                steps_completed += 1
                self.absolute_step_count += 1
                self.steps_since_checkpoint += 1
                self.steps_since_label_reset += 1
                if self.steps_since_checkpoint > 50 and not self.location_tracker_activated:
                    self.location_tracker_activated = True
                    self.location_tracker = {}

                _, location, _ = self.emulator.get_state_from_memory()
                if self.last_location != location:
                    if self.last_coords is not None and self.last_location is not None:
                        self.label_archive.setdefault(self.last_location, {}).setdefault(self.last_coords[1], {})[
                            self.last_coords[0]
                        ] = f"Entrance to {location} (Approximate)"
                    self.steps_since_label_reset = 0
                self.last_location = location

                if self.steps_since_label_reset > 200:
                    self.text_display.add_message("Clearing labels to clear potential bad labels...")
                    self.steps_since_label_reset = 0
                    location_archive = self.label_archive.get(location)
                    if location_archive:
                        for key, value in list(location_archive.items()):
                            for key2, value2 in list(value.items()):
                                if "approximate" not in value2.lower():
                                    del value[key2]
                            if not value:
                                del location_archive[key]

                logger.info(f"Completed step {steps_completed}/{num_steps}")
                self.text_display.add_message(f"Absolute step count: {self.absolute_step_count}")

                if save_file_name is not None and not steps_completed % save_every:
                    self.emulator.save_state(save_file_name)
                    self.save_location_archive(self.location_archive_file_name)
                    with open("location_milestones.txt", "w", encoding="utf-8") as fw:
                        fw.write(str(self.location_milestones))

            except KeyboardInterrupt:
                logger.info("Received keyboard interrupt, stopping")
                self.running = False
            except Exception as exc:
                logger.error(f"Error in agent loop: {exc}")
                raise exc

        if save_file_name is not None:
            logger.info("Saving state")
            self.emulator.save_state(save_file_name)
            self.save_location_archive(self.location_archive_file_name)
            with open("location_milestones.txt", "w", encoding="utf-8") as fw:
                fw.write(str(self.location_milestones))

        if not self.running or self.pyboy_main_thread:
            self.emulator.stop()

        return steps_completed

    def stop(self) -> None:
        self.running = False
        self.emulator.stop()
