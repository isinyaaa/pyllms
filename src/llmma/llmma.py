import asyncio
import os
import statistics
import typing as t
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging import getLogger

from attrs import define, field
from prettytable import PrettyTable

from ._providers import PROVIDERS, Provider
from .providers.base import (
    AsyncProvider,
    AsyncStreamResult,
    Result,
    StreamProvider,
    StreamResult,
    SyncProvider,
    msg_from_raw,
)

LOGGER = getLogger(__name__)


SyncAPIResult = list[Result]
AsyncAPIResult = t.Awaitable[SyncAPIResult]
APIResult = SyncAPIResult | AsyncAPIResult


@define
class LLMMA:
    DEFAULT_MODEL = os.getenv("LLMMA_DEFAULT_MODEL") or "gpt-4o"
    models: dict[str, Provider] = field(factory=dict)

    @classmethod
    def default(cls, **kwargs):
        try:
            return cls().add_model(cls.DEFAULT_MODEL, **kwargs)
        except ValueError as e:
            msg = f"Default model {cls.DEFAULT_MODEL} not found in any provider"
            raise Exception(msg) from e

    def add_model(self, model: str, api_key: str | None, **kwargs):
        provider = next((p for p in PROVIDERS.values() if model in p.kind.MODEL_INFO), None)
        if not provider:
            msg = f"{model} is not registered"
            raise ValueError(msg)
        if provider.api_key_name:
            api_key = api_key or os.getenv(provider.api_key_name)
            if not api_key:
                msg = f"{provider.api_key_name} environment variable is required"
                raise Exception(msg)

        impl = provider.kind(api_key=api_key or "", model=model, **kwargs)
        self.models[impl.model] = impl
        return self

    def add_provider(self, provider_name: str, model: str | None = None, api_key: str | None = None, **kwargs):
        inv_map = {p.casefold().lower(): p for p in PROVIDERS}
        try:
            provider = PROVIDERS[inv_map[provider_name.casefold().lower()]]
        except KeyError as e:
            msg = f"Provider {provider_name} not found among {list(PROVIDERS.keys())}"
            raise ValueError(msg) from e

        if provider.api_key_name:
            api_key = api_key or os.getenv(provider.api_key_name)
            if not api_key:
                msg = f"{provider.api_key_name} environment variable is required"
                raise Exception(msg)

        impl = provider.kind(api_key=api_key or "", model=model, **kwargs)
        self.models[impl.model] = impl
        return self

    def stream_models(self) -> list[StreamProvider]:
        return [m for m in self.models.values() if isinstance(m, StreamProvider)]

    def async_models(self) -> list[AsyncProvider]:
        return [m for m in self.models.values() if isinstance(m, AsyncProvider)]

    def sync_models(self) -> list[SyncProvider]:
        return list(self.models.values())

    def to_list(self, query: str | None = None) -> list[dict[str, t.Any]]:
        return [
            {
                "provider": provider.__class__.__name__,
                "name": model,
                "cost": cost,
            }
            for provider in self.models.values()
            for model, cost in provider.MODEL_INFO.items()
            if not query or (query.lower() in model.lower() or query.lower() in provider.__class__.__name__.lower())
        ]

    def count_tokens(self, content: str | list[dict[str, t.Any]]) -> list[int]:
        return [provider.count_tokens(content) for provider in self.models.values()]

    @staticmethod
    def _prepare_input(
        prompt: str | dict | list[dict],
        history: list[dict] | None = None,
        system_message: str | None = None,
    ) -> list[dict]:
        messages = history or []
        if system_message:
            messages.extend(msg_from_raw(system_message, "system"))
        messages.extend(msg_from_raw(prompt))
        return messages

    def complete(
        self,
        prompt: str | dict | list[dict],
        history: list[dict] | None = None,
        system_message: str | None = None,
        **kwargs: t.Any,
    ) -> SyncAPIResult:
        def _wrap(
            model: SyncProvider,
        ) -> Result:
            messages = self._prepare_input(prompt, history, system_message)
            with model.track_latency():
                response = model.complete(messages, **kwargs)

            completion = response.pop("completion")
            function_call = response.pop("function_call", None)
            kwargs["messages"] = messages

            return Result(
                text=completion,
                model_inputs=kwargs,
                provider=model,
                meta={"latency": model.latency, **response},
                function_call=function_call,
            )

        with ThreadPoolExecutor() as executor:
            return list(executor.map(_wrap, self.sync_models()))

    def acomplete(
        self,
        prompt: str | dict | list[dict],
        history: list[dict] | None = None,
        system_message: str | None = None,
        **kwargs: t.Any,
    ) -> AsyncAPIResult:
        async def _wrap(
            model: AsyncProvider,
        ) -> Result:
            messages = self._prepare_input(prompt, history, system_message)
            with model.track_latency():
                response = await model.acomplete(messages, **kwargs)

            completion = response.pop("completion")
            function_call = response.pop("function_call", None)
            kwargs["messages"] = messages

            return Result(
                text=completion,
                model_inputs=kwargs,
                provider=model,
                meta={"latency": model.latency, **response},
                function_call=function_call,
            )

        async def gather():
            return await asyncio.gather(*[_wrap(p) for p in self.async_models()])

        return gather()

    def complete_stream(
        self,
        prompt: str | dict | list[dict],
        history: list[dict] | None = None,
        system_message: str | None = None,
        **kwargs: t.Any,
    ) -> StreamResult:
        sm = self.stream_models()
        if len(sm) > 1:
            msg = "Streaming is possible only with a single model"
            raise ValueError(msg)

        model = sm[0]
        messages = self._prepare_input(prompt, history, system_message)
        kwargs["messages"] = messages

        # TODO: track latency
        return StreamResult(
            stream=model.complete_stream(messages, **kwargs),
            model_inputs=kwargs,
            provider=model,
        )

    def acomplete_stream(
        self,
        prompt: str | dict | list[dict],
        history: list[dict] | None = None,
        system_message: str | None = None,
        **kwargs: t.Any,
    ) -> AsyncStreamResult:
        sm = self.stream_models()
        if len(sm) > 1:
            msg = "Streaming is possible only with a single model"
            raise ValueError(msg)

        model = sm[0]
        messages = self._prepare_input(prompt, history, system_message)

        # TODO: track latency
        return AsyncStreamResult(
            _stream=model.acomplete_stream(messages, **kwargs),
            model_inputs=kwargs,
            provider=model,
        )

    def benchmark(
        self,
        problems: list[tuple[str, str]] | None = None,
        delay: float = 0,
        evaluator: "LLMMA | None" = None,
        show_outputs: bool = False,
        **kwargs: t.Any,
    ) -> tuple[PrettyTable, PrettyTable | None]:
        from . import _bench as bench

        problems = problems or bench.PROBLEMS

        model_results = {}

        # Run completion tasks in parallel for each model, but sequentially for each prompt within a model
        with ThreadPoolExecutor() as executor:
            fmap = {
                executor.submit(bench.process_prompts_sequentially, model, problems, evaluator, delay, **kwargs): model
                for model in self.models.values()
            }

            for future in as_completed(fmap):
                try:
                    (
                        outputs,
                        equeue,
                        threads,
                    ) = future.result()
                except Exception as e:
                    warnings.warn(f"Error processing results: {str(e)}", stacklevel=2)
                    # Don't add failed models to results
                    continue

                latency = [o["latency"] for o in outputs]
                total_latency = sum(latency)
                tokens = sum([o["tokens"] for o in outputs])
                model = fmap[future]
                model_results[model] = {
                    "outputs": outputs,
                    "total_latency": total_latency,
                    "total_cost": sum([o["cost"] for o in outputs]),
                    "total_tokens": tokens,
                    "evaluation": [None] * len(outputs),
                    "aggregated_speed": tokens / total_latency,
                    "median_latency": statistics.median(latency),
                }

                if evaluator:
                    for t in threads:
                        t.join()

                    # Process all evaluation results
                    while not equeue.empty():
                        i, evaluation = equeue.get()
                        if evaluation:
                            model_results[model]["evaluation"][i] = sum(evaluation)

        def eval(x):
            data = model_results[x]
            return data["aggregated_speed"] * (sum(data["evaluation"]) if evaluator else 1)

        sorted_models = sorted(
            model_results,
            key=eval,
            reverse=True,
        )

        pytable = defaultdict(list)
        for model in sorted_models:
            data = model_results[model]
            total_score = 0
            scores: list[int] = data["evaluation"]
            for i, out in enumerate(data["outputs"]):
                latency = out["latency"]
                tokens = out["tokens"]
                pytable["model"].append(str(model))
                pytable["text"].append(out["text"])
                pytable["tokens"].append(tokens)
                pytable["cost"].append(f"{out['cost']:.5f}")
                pytable["latency"].append(f"{latency:.2f}")
                pytable["speed"].append(f"{(tokens / latency):.2f}")
                if evaluator:
                    score = scores[i]
                    total_score += score
                    score = str(score)
                else:
                    score = "N/A"
                pytable["score"].append(score)

            pytable["model"].append(str(model))
            pytable["text"].append("")
            pytable["tokens"].append(str(data["total_tokens"]))
            pytable["cost"].append(f"{data['total_cost']:.5f}")
            pytable["latency"].append(f"{data['median_latency']:.2f}")
            pytable["speed"].append(f"{data['aggregated_speed']:.2f}")
            if evaluator and len(scores):
                acc = 100 * total_score / len(scores)
                score = f"{acc:.2f}%"
            else:
                score = "N/A"
            pytable["score"].append(score)

        headers = {
            "model": "Model",
            "text": "Output",
            "tokens": "Tokens",
            "cost": "Cost ($)",
            "latency": "Latency (s)",
            "speed": "Speed (tokens/sec)",
            "score": "Evaluation",
        }
        questions_table: PrettyTable | None = None
        if evaluator:
            questions_table = PrettyTable(["Category", "Index", "Question"])
            questions_table.align["Question"] = "l"

            for i, problem in enumerate(problems):
                scores = [m["evaluation"][i] for m in model_results.values()]

                if all(scores):
                    questions_table.add_row(["Easiest", i, self._ellipsize(problem[0])])
                elif not any(scores):
                    questions_table.add_row(["Hardest", i, self._ellipsize(problem[0])])
        else:
            headers.pop("score")
            pytable.pop("score")

        if not show_outputs:
            headers.pop("text")
            pytable.pop("text")
        table = PrettyTable(list(headers.values()))
        table.add_rows(list(zip(*[pytable[k] for k in headers], strict=False)))

        return table, questions_table

    @staticmethod
    def _ellipsize(text: str, max_len: int = 100) -> str:
        return text if len(text) <= max_len else text[: max_len - 3] + "..."
