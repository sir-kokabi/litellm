"""
Provider budget limiting

Use this if you want to set $ budget limits for each provider.

Note: This is a filter, like tag-routing. Meaning it will accept healthy deployments and then filter out deployments that have exceeded their budget limit.

This means you can use this with weighted-pick, lowest-latency, simple-shuffle, routing etc

Example:
```
openai:
	budget_limit: 0.000000000001
	time_period: 1d
anthropic:
	budget_limit: 100
	time_period: 7d
```
"""

import asyncio
from typing import TYPE_CHECKING, Any, Dict, List, Optional, TypedDict, Union

import litellm
from litellm._logging import verbose_router_logger
from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.core_helpers import _get_parent_otel_span_from_kwargs
from litellm.litellm_core_utils.duration_parser import duration_in_seconds
from litellm.router_utils.cooldown_callbacks import (
    _get_prometheus_logger_from_callbacks,
)
from litellm.types.router import (
    LiteLLM_Params,
    ProviderBudgetConfigType,
    ProviderBudgetInfo,
    RouterErrors,
)
from litellm.types.utils import StandardLoggingPayload

if TYPE_CHECKING:
    from opentelemetry.trace import Span as _Span

    Span = _Span
else:
    Span = Any

DEFAULT_REDIS_SYNC_INTERVAL = 60


class ProviderBudgetLimiting(CustomLogger):
    def __init__(self, router_cache: DualCache, provider_budget_config: dict):
        self.router_cache = router_cache
        self.last_synced_values = {}  # To track last synced values for each key
        asyncio.create_task(self.periodic_sync_in_memory_spend_with_redis())

        # cast elements of provider_budget_config to ProviderBudgetInfo
        for provider, config in provider_budget_config.items():
            if config is None:
                raise ValueError(
                    f"No budget config found for provider {provider}, provider_budget_config: {provider_budget_config}"
                )

            if not isinstance(config, ProviderBudgetInfo):
                provider_budget_config[provider] = ProviderBudgetInfo(
                    budget_limit=config.get("budget_limit"),
                    time_period=config.get("time_period"),
                )

        self.provider_budget_config: ProviderBudgetConfigType = provider_budget_config
        verbose_router_logger.debug(
            f"Initalized Provider budget config: {self.provider_budget_config}"
        )

        # Add self to litellm callbacks if it's a list
        if isinstance(litellm.callbacks, list):
            litellm.callbacks.append(self)  # type: ignore

    async def async_filter_deployments(
        self,
        healthy_deployments: Union[List[Dict[str, Any]], Dict[str, Any]],
        request_kwargs: Optional[Dict] = None,
    ):
        """
        Filter out deployments that have exceeded their provider budget limit.


        Example:
        if deployment = openai/gpt-3.5-turbo
            and openai spend > openai budget limit
                then skip this deployment
        """

        # If a single deployment is passed, convert it to a list
        if isinstance(healthy_deployments, dict):
            healthy_deployments = [healthy_deployments]

        # Don't do any filtering if there are no healthy deployments
        if len(healthy_deployments) == 0:
            return healthy_deployments

        potential_deployments: List[Dict] = []

        # Extract the parent OpenTelemetry span for tracing
        parent_otel_span: Optional[Span] = _get_parent_otel_span_from_kwargs(
            request_kwargs
        )

        # Collect all providers and their budget configs
        # {"openai": ProviderBudgetInfo, "anthropic": ProviderBudgetInfo, "azure": None}
        _provider_configs: Dict[str, Optional[ProviderBudgetInfo]] = {}
        for deployment in healthy_deployments:
            provider = self._get_llm_provider_for_deployment(deployment)
            if provider is None:
                continue
            budget_config = self._get_budget_config_for_provider(provider)
            _provider_configs[provider] = budget_config

        # Filter out providers without budget config
        provider_configs: Dict[str, ProviderBudgetInfo] = {
            provider: config
            for provider, config in _provider_configs.items()
            if config is not None
        }

        # Build cache keys for batch retrieval
        cache_keys = []
        for provider, config in provider_configs.items():
            cache_keys.append(f"provider_spend:{provider}:{config.time_period}")

        # Fetch current spend for all providers using batch cache
        _current_spends = await self.router_cache.async_batch_get_cache(
            keys=cache_keys,
            parent_otel_span=parent_otel_span,
        )
        current_spends: List = _current_spends or [0.0] * len(provider_configs)

        # Map providers to their current spend values
        provider_spend_map: Dict[str, float] = {}
        for idx, provider in enumerate(provider_configs.keys()):
            provider_spend_map[provider] = float(current_spends[idx] or 0.0)

        # Filter healthy deployments based on budget constraints
        deployment_above_budget_info: str = ""  # used to return in error message
        for deployment in healthy_deployments:
            provider = self._get_llm_provider_for_deployment(deployment)
            if provider is None:
                continue
            budget_config = provider_configs.get(provider)

            if not budget_config:
                continue

            current_spend = provider_spend_map.get(provider, 0.0)
            budget_limit = budget_config.budget_limit

            verbose_router_logger.debug(
                f"Current spend for {provider}: {current_spend}, budget limit: {budget_limit}"
            )
            self._track_provider_remaining_budget_prometheus(
                provider=provider,
                spend=current_spend,
                budget_limit=budget_limit,
            )

            if current_spend >= budget_limit:
                debug_msg = f"Exceeded budget for provider {provider}: {current_spend} >= {budget_limit}"
                verbose_router_logger.debug(debug_msg)
                deployment_above_budget_info += f"{debug_msg}\n"
                continue

            potential_deployments.append(deployment)

        if len(potential_deployments) == 0:
            raise ValueError(
                f"{RouterErrors.no_deployments_with_provider_budget_routing.value}: {deployment_above_budget_info}"
            )

        return potential_deployments

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """
        Increment provider spend in DualCache (InMemory + Redis)

        Handles saving current provider spend to Redis.

        Spend is stored as:
            provider_spend:{provider}:{time_period}
            ex. provider_spend:openai:1d
            ex. provider_spend:anthropic:7d

        The time period is tracked for time_periods set in the provider budget config.
        """
        verbose_router_logger.debug("in ProviderBudgetLimiting.async_log_success_event")
        standard_logging_payload: Optional[StandardLoggingPayload] = kwargs.get(
            "standard_logging_object", None
        )
        if standard_logging_payload is None:
            raise ValueError("standard_logging_payload is required")

        response_cost: float = standard_logging_payload.get("response_cost", 0)

        custom_llm_provider: str = kwargs.get("litellm_params", {}).get(
            "custom_llm_provider", None
        )
        if custom_llm_provider is None:
            raise ValueError("custom_llm_provider is required")

        budget_config = self._get_budget_config_for_provider(custom_llm_provider)
        if budget_config is None:
            raise ValueError(
                f"No budget config found for provider {custom_llm_provider}, self.provider_budget_config: {self.provider_budget_config}"
            )

        spend_key = f"provider_spend:{custom_llm_provider}:{budget_config.time_period}"
        ttl_seconds = duration_in_seconds(duration=budget_config.time_period)
        verbose_router_logger.debug(
            f"Incrementing spend for {spend_key} by {response_cost}, ttl: {ttl_seconds}"
        )
        # Increment the spend in Redis and set TTL
        await self.router_cache.in_memory_cache.async_increment(
            key=spend_key,
            value=response_cost,
        )
        verbose_router_logger.debug(
            f"Incremented spend for {spend_key} by {response_cost}, ttl: {ttl_seconds}"
        )

    async def periodic_sync_in_memory_spend_with_redis(self):
        """
        Handler that triggers sync_in_memory_spend_with_redis every DEFAULT_REDIS_SYNC_INTERVAL seconds

        Required for multi-instance environment usage of provider budgets
        """
        while True:
            try:
                await self._sync_in_memory_spend_with_redis()
                await asyncio.sleep(
                    DEFAULT_REDIS_SYNC_INTERVAL
                )  # Wait for 5 seconds before next sync
            except Exception as e:
                verbose_router_logger.error(f"Error in periodic sync task: {str(e)}")
                await asyncio.sleep(
                    DEFAULT_REDIS_SYNC_INTERVAL
                )  # Still wait 5 seconds on error before retrying

    async def _push_in_memory_increments_to_redis(self):
        """
        This is a latency / speed optimization.

        How this works:
        - Collect all provider spend increments in `router_cache.in_memory_cache`, done in async_log_success_event
        - Push all increments to Redis in this function
        - Reset the in-memory `last_synced_values`
        """
        try:
            if not self.router_cache.redis_cache:
                return  # Redis is not initialized

            # Build cache keys for all providers
            cache_keys = [
                f"provider_spend:{provider}:{config.time_period}"
                for provider, config in self.provider_budget_config.items()
                if config is not None
            ]

            # Fetch current in-memory values
            current_values = (
                await self.router_cache.in_memory_cache.async_batch_get_cache(
                    keys=cache_keys
                )
            )

            for key, current_value in zip(cache_keys, current_values):
                if current_value is None:
                    continue  # Skip keys with no in-memory value

                # Get the last synced value (default to 0 if not synced before)
                last_synced = self.last_synced_values.get(key, 0.0)
                # Calculate the delta to push to Redis
                delta = float(current_value) - last_synced
                if delta > 0:  # Only push if there is a positive increment
                    await self.router_cache.redis_cache.async_increment(
                        key=key,
                        value=delta,
                    )
                    verbose_router_logger.debug(
                        f"Pushed delta to Redis for {key}: {delta} (last synced: {last_synced}, current: {current_value})"
                    )

                    # Update last synced value
                    self.last_synced_values[key] = float(current_value)

        except Exception as e:
            verbose_router_logger.error(
                f"Error syncing in-memory cache with Redis: {str(e)}"
            )

    async def _sync_in_memory_spend_with_redis(self):
        """
        Ensures in-memory cache is updated with latest Redis values for all provider spends.

        Why Do we need this?
        - Redis is our source of truth for provider spend
        - Optimization to hit ~100ms latency. Performance was impacted when redis was used for read/write per request


        In a multi-instance evironment, each instance needs to periodically get the provider spend from Redis to ensure it is consistent across all instances.
        """

        try:
            # No need to sync if Redis cache is not initialized
            if self.router_cache.redis_cache is None:
                return

            # Push all provider spend increments to Redis
            await self._push_in_memory_increments_to_redis()

            # Handle Reading all current provider spend from Redis in Memory
            # Get all providers and their budget configs
            cache_keys = []
            for provider, config in self.provider_budget_config.items():
                if config is None:
                    continue
                cache_keys.append(f"provider_spend:{provider}:{config.time_period}")

            # Batch fetch current spend values from Redis
            redis_values = await self.router_cache.redis_cache.async_batch_get_cache(
                key_list=cache_keys
            )

            # Update in-memory cache with Redis values
            if isinstance(redis_values, dict):  # Check if redis_values is a dictionary
                for key, value in redis_values.items():
                    if value is not None:
                        await self.router_cache.in_memory_cache.async_set_cache(
                            key=key, value=float(value)
                        )
                        verbose_router_logger.debug(
                            f"Updated in-memory cache for {key}: {value}"
                        )

        except Exception as e:
            verbose_router_logger.error(
                f"Error syncing in-memory cache with Redis: {str(e)}"
            )

    def _get_budget_config_for_provider(
        self, provider: str
    ) -> Optional[ProviderBudgetInfo]:
        return self.provider_budget_config.get(provider, None)

    def _get_llm_provider_for_deployment(self, deployment: Dict) -> Optional[str]:
        try:
            _litellm_params: LiteLLM_Params = LiteLLM_Params(
                **deployment.get("litellm_params", {"model": ""})
            )
            _, custom_llm_provider, _, _ = litellm.get_llm_provider(
                model=_litellm_params.model,
                litellm_params=_litellm_params,
            )
        except Exception:
            verbose_router_logger.error(
                f"Error getting LLM provider for deployment: {deployment}"
            )
            return None
        return custom_llm_provider

    def _track_provider_remaining_budget_prometheus(
        self, provider: str, spend: float, budget_limit: float
    ):
        """
        Optional helper - emit provider remaining budget metric to Prometheus

        This is helpful for debugging and monitoring provider budget limits.
        """
        from litellm.integrations.prometheus import PrometheusLogger

        prometheus_logger = _get_prometheus_logger_from_callbacks()
        if prometheus_logger:
            prometheus_logger.track_provider_remaining_budget(
                provider=provider,
                spend=spend,
                budget_limit=budget_limit,
            )
