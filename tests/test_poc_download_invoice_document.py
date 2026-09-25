from __future__ import annotations

import sys
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import poc_download_invoice_document as f05  # noqa: E402


class FakeActionElement:
    def __init__(self, supports_invoke: bool, invoke_raises: bool = False) -> None:
        self.iface_invoke = object() if supports_invoke else None
        self.invoke_raises = invoke_raises
        self.invoke_calls = 0
        self.click_calls = 0

    def invoke(self) -> None:
        self.invoke_calls += 1
        if self.invoke_raises:
            raise RuntimeError("ambiguous invoke failure")

    def click_input(self) -> None:
        self.click_calls += 1


class FakeLiveElement:
    def __init__(
        self,
        *,
        name: str = "",
        control_type: str = "Pane",
        automation_id: str = "",
        class_name: str = "",
        process_id: int = 123,
        runtime_id: tuple[int, ...] = (),
        visible: bool = True,
        enabled: bool = True,
        children: list["FakeLiveElement"] | None = None,
    ) -> None:
        self.element_info = SimpleNamespace(
            name=name,
            control_type=control_type,
            automation_id=automation_id,
            class_name=class_name,
            process_id=process_id,
            runtime_id=runtime_id,
        )
        self._visible = visible
        self._enabled = enabled
        self._children = children or []

    def is_visible(self) -> bool:
        return self._visible

    def is_enabled(self) -> bool:
        return self._enabled

    def children(self) -> list["FakeLiveElement"]:
        return self._children

    def rectangle(self) -> SimpleNamespace:
        return SimpleNamespace(left=10, top=10, right=210, bottom=110)


def test_action_failure_never_falls_through_to_second_mutation() -> None:
    element = FakeActionElement(supports_invoke=True, invoke_raises=True)

    with pytest.raises(RuntimeError, match="ambiguous invoke failure"):
        f05.activate_preselected_once(element)

    assert element.invoke_calls == 1
    assert element.click_calls == 0


def test_click_is_selected_before_single_mutation() -> None:
    element = FakeActionElement(supports_invoke=False)

    method = f05.activate_preselected_once(element)

    assert method == "click_input"
    assert element.invoke_calls == 0
    assert element.click_calls == 1


def test_light_wait_can_resume_an_existing_dialog() -> None:
    existing = SimpleNamespace(handle=17, title="Guardar como")

    result = f05._light_wait_for_window(
        lambda: [existing],
        before_handles={17},
        predicate=lambda window: window.title == "Guardar como",
        timeout_seconds=0.01,
        poll_interval=0.001,
    )

    assert result is existing


def test_light_wait_can_require_a_new_dialog() -> None:
    existing = SimpleNamespace(handle=17, title="Guardar como")
    new = SimpleNamespace(handle=18, title="Guardar como")
    snapshots = iter([[existing], [existing, new]])

    result = f05._light_wait_for_window(
        lambda: next(snapshots, [existing, new]),
        before_handles={17},
        predicate=lambda window: window.title == "Guardar como",
        timeout_seconds=0.1,
        poll_interval=0.001,
        require_new=True,
    )

    assert result is new


def test_wait_for_file_requires_consecutive_stable_reads(tmp_path: Path) -> None:
    output = tmp_path / "invoice.pdf"
    output.write_bytes(b"%PDF-stable")

    result = f05.wait_for_file_stable(
        output,
        poll_interval=0.001,
        timeout_seconds=0.1,
        stable_reads_required=3,
    )

    assert result.appeared is True
    assert result.stabilized is True
    assert result.size_bytes == len(b"%PDF-stable")


def test_pdf_validation_requires_at_least_one_page(tmp_path: Path) -> None:
    valid = tmp_path / "valid.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with valid.open("wb") as stream:
        writer.write(stream)

    header_valid, pages_valid, page_count = f05.validate_pdf_file(valid)

    assert header_valid is True
    assert pages_valid is True
    assert page_count == "1"


def test_zero_page_pdf_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    writer = PdfWriter()
    with empty.open("wb") as stream:
        writer.write(stream)

    header_valid, pages_valid, page_count = f05.validate_pdf_file(empty)

    assert header_valid is True
    assert pages_valid is False
    assert page_count == "0"


def test_viewer_receipt_binds_process_handle_and_invoice(tmp_path: Path) -> None:
    receipt = tmp_path / "viewer.json"
    f05.save_viewer_receipt("INV-001", os.getpid(), 77, receipt)

    assert f05.viewer_receipt_matches("INV-001", os.getpid(), 77, receipt) is True
    assert f05.viewer_receipt_matches("INV-002", os.getpid(), 77, receipt) is False
    assert "INV-001" not in receipt.read_text(encoding="utf-8")


def test_pdf_file_requires_exact_visible_menu_item(monkeypatch: pytest.MonkeyPatch) -> None:
    exact = FakeLiveElement(
        name="PDF File",
        control_type="MenuItem",
        runtime_id=(1, 2),
    )
    wrong_type = FakeLiveElement(
        name="PDF File",
        control_type="Button",
        runtime_id=(1, 3),
    )
    similar = FakeLiveElement(
        name="PDF File (default)",
        control_type="MenuItem",
        runtime_id=(1, 4),
    )
    popup = FakeLiveElement(
        control_type="Menu",
        class_name="#32768",
        runtime_id=(1,),
        children=[exact, wrong_type, similar],
    )
    desktop = SimpleNamespace(windows=lambda: [popup])
    monkeypatch.setattr("pywinauto.Desktop", lambda backend: desktop)

    matches, roots = f05.find_pdf_file_menu_candidates(
        expected_pid=123,
        viewer=FakeLiveElement(process_id=123, runtime_id=(9,)),
        timeout_seconds=0.05,
    )

    assert matches == [exact]
    assert roots == [popup]
