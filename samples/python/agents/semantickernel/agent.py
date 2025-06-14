import asyncio
import logging
import os

from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Annotated, Any, Literal

import httpx

from dotenv import load_dotenv
from pydantic import BaseModel
from semantic_kernel.agents import ChatCompletionAgent, ChatHistoryAgentThread
from semantic_kernel.connectors.ai.open_ai import (
    OpenAIChatCompletion,
    OpenAIChatPromptExecutionSettings,
)
from semantic_kernel.contents import (
    FunctionCallContent,
    FunctionResultContent,
    StreamingChatMessageContent,
    StreamingTextContent,
)
from semantic_kernel.functions import KernelArguments, kernel_function


if TYPE_CHECKING:
    from semantic_kernel.contents import ChatMessageContent

logger = logging.getLogger(__name__)

load_dotenv()

# region Plugin


class CurrencyPlugin:
    """A simple currency plugin that leverages Frankfurter for exchange rates.

    The Plugin is used by the `currency_exchange_agent`.
    """

    @kernel_function(
        description='Retrieves exchange rate between currency_from and currency_to using Frankfurter API'
    )
    def get_exchange_rate(
        self,
        currency_from: Annotated[
            str, 'Currency code to convert from, e.g. USD'
        ],
        currency_to: Annotated[
            str, 'Currency code to convert to, e.g. EUR or INR'
        ],
        date: Annotated[str, "Date or 'latest'"] = 'latest',
    ) -> str:
        try:
            response = httpx.get(
                f'https://api.frankfurter.app/{date}',
                params={'from': currency_from, 'to': currency_to},
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            if 'rates' not in data or currency_to not in data['rates']:
                return f'Could not retrieve rate for {currency_from} to {currency_to}'
            rate = data['rates'][currency_to]
            return f'1 {currency_from} = {rate} {currency_to}'
        except Exception as e:
            return f'Currency API call failed: {str(e)}'


# endregion

# region Response Format


class ResponseFormat(BaseModel):
    """A Response Format model to direct how the model should respond."""

    status: Literal['input_required', 'completed', 'error'] = 'input_required'
    message: str


# endregion

# region Semantic Kernel Agent


class SemanticKernelTravelAgent:
    """Wraps Semantic Kernel-based agents to handle Travel related tasks."""

    agent: ChatCompletionAgent
    thread: ChatHistoryAgentThread = None
    SUPPORTED_CONTENT_TYPES = ['text', 'text/plain']

    def __init__(self):
        # Default values that can be overridden from environment variables
        api_key = os.getenv("OPENAI_API_KEY", "api_key")
        model_id = os.getenv("OPENAI_MODEL_ID", "qwen3-0.6b")
        base_url = os.getenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
        
        # Create a shared OpenAI client with custom base_url
        import openai
        openai_client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url  # 使用 base_url 连接本地服务
        )
        
        # Define a CurrencyExchangeAgent to handle currency-related tasks
        currency_exchange_agent = ChatCompletionAgent(
            service=OpenAIChatCompletion(
                ai_model_id=model_id,
                api_key=api_key,
                async_client=openai_client  # 使用预配置的 client
            ),
            name='CurrencyExchangeAgent',
            instructions=(
                'You specialize in handling currency-related requests from travelers. '
                'This includes providing current exchange rates, converting amounts between different currencies, '
                'explaining fees or charges related to currency exchange, and giving advice on the best practices for exchanging currency. '
                'Your goal is to assist travelers promptly and accurately with all currency-related questions.'
            ),
            plugins=[CurrencyPlugin()],
        )

        # Define an ActivityPlannerAgent to handle activity-related tasks
        activity_planner_agent = ChatCompletionAgent(
            service=OpenAIChatCompletion(
                ai_model_id=model_id,
                api_key=api_key,
                async_client=openai_client  # 使用预配置的 client
            ),
            name='ActivityPlannerAgent',
            instructions=(
                'You specialize in planning and recommending activities for travelers. '
                'This includes suggesting sightseeing options, local events, dining recommendations, '
                'booking tickets for attractions, advising on travel itineraries, and ensuring activities '
                'align with traveler preferences and schedule. '
                'Your goal is to create enjoyable and personalized experiences for travelers.'
            ),
        )

        # Define the main TravelManagerAgent to delegate tasks to the appropriate agents
        self.agent = ChatCompletionAgent(
            service=OpenAIChatCompletion(
                ai_model_id=model_id,
                api_key=api_key,
                async_client=openai_client  # 使用预配置的 client
            ),
            name='TravelManagerAgent',
            instructions=(
                "Your role is to carefully analyze the traveler's request and forward it to the appropriate agent based on the "
                'specific details of the query. '
                'Forward any requests involving monetary amounts, currency exchange rates, currency conversions, fees related '
                'to currency exchange, financial transactions, or payment methods to the CurrencyExchangeAgent. '
                'Forward requests related to planning activities, sightseeing recommendations, dining suggestions, event '
                'booking, itinerary creation, or any experiential aspects of travel that do not explicitly involve monetary '
                'transactions to the ActivityPlannerAgent. '
                'Your primary goal is precise and efficient delegation to ensure travelers receive accurate and specialized '
                'assistance promptly.'
            ),
            plugins=[currency_exchange_agent, activity_planner_agent],
            arguments=KernelArguments(
                settings=OpenAIChatPromptExecutionSettings(
                    response_format=ResponseFormat,
                )
            ),
        )

    async def invoke(self, user_input: str, session_id: str) -> dict[str, Any]:
        """Handle synchronous tasks (like tasks/send).

        Args:
            user_input (str): User input message.
            session_id (str): Unique identifier for the session.

        Returns:
            dict: A dictionary containing the content, task completion status,
            and user input requirement.
        """
        await self._ensure_thread_exists(session_id)

        # Use SK's get_response for a single shot
        response = await self.agent.get_response(
            messages=user_input,
            thread=self.thread,
        )
        return self._get_agent_response(response.content)

    async def stream(
        self,
        user_input: str,
        session_id: str,
    ) -> AsyncIterable[dict[str, Any]]:
        """For streaming tasks we yield the SK agent's invoke_stream progress.

        Args:
            user_input (str): User input message.
            session_id (str): Unique identifier for the session.

        Yields:
            dict: A dictionary containing the content, task completion status,
            and user input requirement.
        """
        await self._ensure_thread_exists(session_id)

        plugin_notice_seen = False
        plugin_event = asyncio.Event()

        text_notice_seen = False
        chunks: list[StreamingChatMessageContent] = []

        async def _handle_intermediate_message(
            message: 'ChatMessageContent',
        ) -> None:
            """Handle intermediate messages from the agent."""
            nonlocal plugin_notice_seen
            if not plugin_notice_seen:
                plugin_notice_seen = True
                plugin_event.set()
            # An example of handling intermediate messages during function calling
            for item in message.items or []:
                if isinstance(item, FunctionResultContent):
                    print(
                        f'SK Function Result:> {item.result} for function: {item.name}'
                    )
                elif isinstance(item, FunctionCallContent):
                    print(
                        f'SK Function Call:> {item.name} with arguments: {item.arguments}'
                    )
                else:
                    print(f'SK Message:> {item}')

        async for chunk in self.agent.invoke_stream(
            messages=user_input,
            thread=self.thread,
            on_intermediate_message=_handle_intermediate_message,
        ):
            if plugin_event.is_set():
                yield {
                    'is_task_complete': False,
                    'require_user_input': False,
                    'content': 'Processing function calls...',
                }
                plugin_event.clear()

            if any(isinstance(i, StreamingTextContent) for i in chunk.items):
                if not text_notice_seen:
                    yield {
                        'is_task_complete': False,
                        'require_user_input': False,
                        'content': 'Building the output...',
                    }
                    text_notice_seen = True
                chunks.append(chunk.message)

        if chunks:
            yield self._get_agent_response(sum(chunks[1:], chunks[0]))

    def _get_agent_response(
        self, message: 'ChatMessageContent'
    ) -> dict[str, Any]:
        """Extracts the structured response from the agent's message content.

        Args:
            message (ChatMessageContent): The message content from the agent.

        Returns:
            dict: A dictionary containing the content, task completion status, and user input requirement.
        """
        structured_response = ResponseFormat.model_validate_json(
            message.content
        )

        default_response = {
            'is_task_complete': False,
            'require_user_input': True,
            'content': 'We are unable to process your request at the moment. Please try again.',
        }

        if isinstance(structured_response, ResponseFormat):
            response_map = {
                'input_required': {
                    'is_task_complete': False,
                    'require_user_input': True,
                },
                'error': {
                    'is_task_complete': False,
                    'require_user_input': True,
                },
                'completed': {
                    'is_task_complete': True,
                    'require_user_input': False,
                },
            }

            response = response_map.get(structured_response.status)
            if response:
                return {**response, 'content': structured_response.message}

        return default_response

    async def _ensure_thread_exists(self, session_id: str) -> None:
        """Ensure the thread exists for the given session ID.

        Args:
            session_id (str): Unique identifier for the session.
        """
        if self.thread is None or self.thread.id != session_id:
            await self.thread.delete() if self.thread else None
            self.thread = ChatHistoryAgentThread(thread_id=session_id)


# endregion
