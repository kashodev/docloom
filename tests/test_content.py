"""Content-source contract tests.

A pack declares where its content comes from, which is what decides whether it
can be generated offline with no key. The invoice pack is procedural (and so
local-first); a text-heavy pack declares LLM_BACKED and implements the builder
contract, which the kernel drives through the provider mix and catalogue runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from decimal import Decimal as D

import pytest

import docloom.packs  # noqa: F401 - registers the invoice pack
from docloom.core import (
    ContentCapability,
    ContentMode,
    LlmContentBuilder,
    build_catalogue,
    capability_of,
    get_pack,
)
from docloom.core.providers import (
    CatalogueItem,
    CompletionRequest,
    ProviderMix,
)
from docloom.core.providers.base import CompletionResult, Usage
from docloom.core.providers.pricing import pricing_for


def run(coro):
    return asyncio.run(coro)


# ── The declared capability ─────────────────────────────────────────────────
def test_invoice_pack_declares_procedural_and_is_local_first() -> None:
    capability = get_pack("invoice").content_capability
    assert capability.mode is ContentMode.PROCEDURAL
    assert capability.local_first is True
    assert capability.requires_api_key is False


def test_llm_backed_capability_is_not_local_first_and_needs_a_key() -> None:
    capability = ContentCapability(ContentMode.LLM_BACKED)
    assert capability.local_first is False
    assert capability.requires_api_key is True


def test_capability_of_defaults_to_procedural_for_an_undeclared_pack() -> None:
    """A pack written before this contract still loads."""
    class OldPack:
        name = "old"

    assert capability_of(OldPack()).mode is ContentMode.PROCEDURAL
    assert capability_of(OldPack()).local_first is True


def test_capability_of_reads_a_declared_pack() -> None:
    assert capability_of(get_pack("invoice")).mode is ContentMode.PROCEDURAL


# ── Driving an LLM-backed pack's catalogue step ─────────────────────────────
class StubProvider:
    pricing = pricing_for("__local__")
    name = "stub"
    model = "stub"

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        return CompletionResult(f"generated:{request.prompt}", Usage(1, 1),
                                self.model, self.name, D(0))

    def estimate_cost(self, request: CompletionRequest) -> D:
        return D(0)


class ContractPack:
    """A minimal text-heavy pack: LLM_BACKED + the builder contract."""

    name = "contract"

    def __init__(self) -> None:
        self.catalogue: dict[str, str] = {}

    @property
    def content_capability(self) -> ContentCapability:
        return ContentCapability(ContentMode.LLM_BACKED, notes="clauses need an LLM")

    def catalogue_items(self) -> list[CatalogueItem]:
        return [
            CatalogueItem(f"clause-{i}", CompletionRequest(system="draft", prompt=f"clause {i}"))
            for i in range(5)
        ]

    def ingest(self, results: Mapping[str, CompletionResult]) -> None:
        self.catalogue = {k: v.text for k, v in results.items()}


def test_contract_pack_satisfies_the_builder_protocol() -> None:
    assert isinstance(ContractPack(), LlmContentBuilder)
    assert not isinstance(get_pack("invoice"), LlmContentBuilder)


def test_build_catalogue_generates_and_ingests() -> None:
    pack = ContractPack()
    mix = ProviderMix([StubProvider()], [1.0])
    report = run(build_catalogue(pack, mix))
    assert len(report.results) == 5
    # The pack took the generated text back into its catalogue.
    assert len(pack.catalogue) == 5
    assert pack.catalogue["clause-0"] == "generated:clause 0"


def test_build_catalogue_refuses_a_procedural_pack() -> None:
    mix = ProviderMix([StubProvider()], [1.0])
    with pytest.raises(ValueError, match="no LLM catalogue to build"):
        run(build_catalogue(get_pack("invoice"), mix))


def test_build_catalogue_rejects_a_declared_pack_that_lacks_the_builder() -> None:
    class Incomplete:
        name = "incomplete"
        content_capability = ContentCapability(ContentMode.LLM_BACKED)

    mix = ProviderMix([StubProvider()], [1.0])
    with pytest.raises(TypeError, match="LlmContentBuilder"):
        run(build_catalogue(Incomplete(), mix))
