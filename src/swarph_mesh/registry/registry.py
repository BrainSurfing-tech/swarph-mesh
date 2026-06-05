"""AdapterRegistry — probe-validated register + $0-first resolve (P2 T3).

The registry is the open adapter contract's runtime: adapters are registered
only after surviving :func:`~swarph_mesh.registry.probes.probe_adapter`
(fail-loud, NO partial load on any gate ``FAIL``), and a request is resolved
to a lane intent-first then $0-first — a subscription/local zero-cost lane
always wins over a metered one when present, and a metered lane is only
reachable when ``budget_ok``.

Two filters shape the candidate set before billing-class ordering:

* **intent filter** — only specs whose ``intents`` contains the request intent.
* **cap filter** — a ``cli`` spec whose ``max_prompt_bytes`` is below the
  request's ``prompt_bytes`` is dropped (the agy-argv-cap class: a long prompt
  silently overflowing a hard argv cap must NOT route to that lane).

When nothing survives, :class:`NoLaneError` is raised — LOUD, never a silent
no-op.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from swarph_mesh.exceptions import AdapterError
from swarph_mesh.registry.probes import GateResult, GateStatus, probe_adapter
from swarph_mesh.registry.spec import AdapterSpec

_ZERO_BILLING = ("subscription_zero", "local_zero")


class NoLaneError(Exception):
    """No registered adapter can serve a request — raised LOUD, never silent."""


class AdapterRegistry:
    """Register probe-validated adapters and resolve a request to a lane.

    ``on_register`` is an optional hook called with each ``spec`` after a
    successful registration (T6 wires the capability indexer here).
    """

    def __init__(self, on_register: Optional[Callable[[AdapterSpec], None]] = None):
        self._on_register = on_register
        # name -> (spec, probe_inputs, results)
        self._entries: dict[str, tuple[AdapterSpec, dict, dict[str, GateResult]]] = {}

    def register(
        self,
        spec: AdapterSpec,
        *,
        runner=None,
        provenance_path=None,
        config_home=None,
    ) -> None:
        """Probe ``spec`` and store it iff no gate ``FAIL``s.

        Runs :func:`probe_adapter` with the supplied inputs. Any gate whose
        status is ``FAIL`` raises :class:`AdapterError` and NOTHING is stored
        (fail-loud, no partial load). ``BLOCKED``/``SKIP`` are permitted.
        """
        probe_inputs = dict(
            runner=runner, provenance_path=provenance_path, config_home=config_home
        )
        results = probe_adapter(spec, **probe_inputs)
        failed = [r for r in results.values() if r.status is GateStatus.FAIL]
        if failed:
            detail = "; ".join(f"{r.gate}: {r.detail}" for r in failed)
            raise AdapterError(
                f"adapter {spec.name!r} failed {len(failed)} gate(s): {detail}"
            )
        self._entries[spec.name] = (spec, probe_inputs, results)
        if self._on_register is not None:
            self._on_register(spec)

    def resolve(
        self,
        intent: str,
        *,
        prompt_bytes: int,
        model: Optional[str] = None,
        budget_ok: bool = False,
    ) -> AdapterSpec:
        """Resolve ``intent`` to a lane: intent-first, cap-filtered, $0-first."""
        cands = [s for (s, _, _) in self._entries.values() if intent in s.intents]

        # CAP FILTER (the agy-cap class): drop cli specs whose hard byte cap is
        # below the request prompt size.
        cands = [
            s
            for s in cands
            if not (
                s.kind == "cli"
                and s.max_prompt_bytes is not None
                and s.max_prompt_bytes < prompt_bytes
            )
        ]

        # $0-FIRST: a zero-cost lane always wins when present.
        zero = [s for s in cands if s.billing_class in _ZERO_BILLING]
        if zero:
            return self._best(zero, model)

        metered = [s for s in cands if s.billing_class == "metered"]
        if metered and budget_ok:
            return self._best(metered, model)

        raise NoLaneError(
            f"no lane for intent={intent} prompt_bytes={prompt_bytes} model={model}"
        )

    @staticmethod
    def _best(cands: list[AdapterSpec], model: Optional[str]) -> AdapterSpec:
        if model is not None:
            for s in cands:
                if s.name == model or model in s.models:
                    return s
        return cands[0]

    def probe_all(self) -> dict[str, dict[str, GateResult]]:
        """Re-probe every registered adapter IN PARALLEL, fail-loud.

        science-claude seat-A persistence ruling: a registered adapter's gates
        are re-verified (not trusted from register-time) concurrently via a
        thread pool. ANY gate ``FAIL`` raises :class:`AdapterError`. Results are
        cached in-memory on the instance and returned keyed by adapter name.
        """
        names = list(self._entries)
        if not names:
            return {}

        def _probe(name: str) -> tuple[str, dict[str, GateResult]]:
            spec, probe_inputs, _ = self._entries[name]
            return name, probe_adapter(spec, **probe_inputs)

        out: dict[str, dict[str, GateResult]] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as ex:
            for name, results in ex.map(_probe, names):
                out[name] = results

        failures = []
        for name, results in out.items():
            for r in results.values():
                if r.status is GateStatus.FAIL:
                    failures.append(f"{name}/{r.gate}: {r.detail}")
        if failures:
            raise AdapterError("probe_all found gate FAIL(s): " + "; ".join(failures))

        # cache + refresh stored results
        for name, results in out.items():
            spec, probe_inputs, _ = self._entries[name]
            self._entries[name] = (spec, probe_inputs, results)
        self._last_probe_all = out
        return out
