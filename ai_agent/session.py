from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Optional

class Phase(Enum):
    IDLE  = "idle"
    SPEC  = "spec"
    WRITE = "write"
    TEST  = "test"


@dataclass
class Port:
    name: str
    direction: Literal["input", "output", "inout"]
    width: int = 1   # 1 = scalar, >1 = bus width


@dataclass
class Spec:
    """Frozen handoff between Spec agent and Writer agents."""
    module_name: str
    ports: list[Port] = field(default_factory=list)
    behavior: str = ""           # natural-language description
    clock: str = "clk"
    reset: Optional[str] = "rst"
    rtl_path: Optional[Path] = None
    tb_path: Optional[Path] = None
    test_strategy: str = ""      # what TB should verify


@dataclass
class TestStatus:
    name: str
    status: Literal["pending", "verified", "failed"] = "pending"
    reason: str = ""


@dataclass
class TestResult:
    phase: Literal["lint", "build", "sim", "waveform", "pass"]
    passed: bool
    stdout: str = ""
    stderr: str = ""
    waveform_path: Optional[Path] = None
    tests: list[TestStatus] = field(default_factory=list)


@dataclass
class Session:
    """In-memory state for one REPL session. Lost on /exit."""
    messages: list[dict] = field(default_factory=list)   # OpenAI-style: role + content (+ tool_calls / tool_call_id)
    phase: Phase = Phase.IDLE
    models: dict[str, str] = field(default_factory=dict) # role -> "provider/model"
    spec: Optional[Spec] = None
    last_test: Optional[TestResult] = None
    tests: list[TestStatus] = field(default_factory=list)
    retry_count: int = 0

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: Optional[str] = None, tool_calls: Optional[list[dict]] = None) -> None:
        """Append an assistant turn. tool_calls in OpenAI format: [{"id","type":"function","function":{"name","arguments":json_str}}]"""
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """OpenAI-style tool result message."""
        self.messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": content})

    def reset_retry(self) -> None:
        """Reset the retry counter to zero."""
        self.retry_count = 0

    def reset(self) -> None:
        """Wipe history + state. Keep models config."""
        self.messages.clear()
        self.phase = Phase.IDLE
        self.spec = None
        self.last_test = None
        self.retry_count = 0

    def set_phase(self, phase: Phase) -> None:
        self.phase = phase
