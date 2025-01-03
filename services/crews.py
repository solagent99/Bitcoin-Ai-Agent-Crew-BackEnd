import asyncio
import os
from backend.factory import backend
from backend.models import AgentFilter, Profile, TaskFilter
from crewai import Agent, Crew, Process, Task
from crewai.agents.parser import AgentAction, AgentFinish
from crewai.tasks.task_output import TaskOutput
from dotenv import load_dotenv
from langchain.agents import AgentExecutor, initialize_agent
from langchain.agents.types import AgentType
from langchain.callbacks.base import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI
from langgraph.graph import END, Graph
from lib.logger import configure_logger
from textwrap import dedent
from tools.tools_factory import filter_crewai_tools_by_names, initialize_tools
from typing import Any, Dict, List, Tuple, Union
from uuid import UUID

logger = configure_logger(__name__)

load_dotenv()

AIBTC_CREWAI_VERBOSE = os.getenv("AIBTC_CREWAI_VERBOSE", "false").lower() == "true"

# Common configurations
MANAGER_AGENT_CONFIG = {
    "role": "Task Manager",
    "goal": "Refine and manage tasks for the crew and assign them memory with a key to store their output.",
    "backstory": "You are responsible for optimizing the crew's workflow and ensuring tasks are well-structured.",
    "verbose": AIBTC_CREWAI_VERBOSE,
    "memory": True,
    "tools": [],
}


def create_manager_agent() -> Agent:
    """Create a standard manager agent with common configuration."""
    return Agent(**MANAGER_AGENT_CONFIG)


def create_agent(agent_data: Dict, tools_map: Dict) -> Agent:
    """Create an agent from agent data and tools map."""
    agent_tools = filter_crewai_tools_by_names(
        agent_data.get("agent_tools", []), tools_map
    )
    return Agent(
        role=agent_data.get("role"),
        goal=agent_data.get("goal"),
        backstory=agent_data.get("backstory"),
        verbose=AIBTC_CREWAI_VERBOSE,
        memory=True,
        allow_delegation=False,
        tools=agent_tools,
    )


def create_task(task_data: Dict, agent: Agent, input_str: str = None) -> Task:
    """Create a task from task data and agent."""
    task_description = task_data.get("description")
    task_expected_output = task_data.get("expected_output")

    if input_str:
        task_description = f"{task_description}\n\nuser_input: {input_str}"

    refined_task_description = f"Refined by Manager: {str(task_description)}"
    refined_task_expected_output = (
        f"Manager's expected outcome: {str(task_expected_output)}"
    )

    return Task(
        description=refined_task_description,
        expected_output=refined_task_expected_output,
        agent=agent,
        async_execution=False,
    )


def create_crew(agents: List[Agent], tasks: List[Task], **kwargs) -> Crew:
    """Create a crew with common configuration."""
    return Crew(
        agents=agents,
        tasks=tasks,
        manager_agent=create_manager_agent(),
        process=Process.sequential,
        memory=True,
        verbose=AIBTC_CREWAI_VERBOSE,
        **kwargs,
    )


def fetch_crew_data(crew_id: UUID) -> Tuple[List, List]:
    """Fetch agents and tasks for the specified crew from Supabase."""

    agents_response = backend.list_agents(filters=AgentFilter(crew_id=crew_id))
    tasks_response = backend.list_tasks(filters=TaskFilter(crew_id=crew_id))

    if not agents_response or not tasks_response:
        raise ValueError("No agents or tasks found for the specified crew.")
    return agents_response, tasks_response


def build_agents_dict(agents_data: List, tools_map: Dict) -> Dict[UUID, Agent]:
    """Build a dictionary of agents from agents data."""
    return {
        agent_data["id"]: create_agent(agent_data, tools_map)
        for agent_data in agents_data
    }


def build_tasks_list(
    tasks_data: List, agents: Dict[UUID, Agent], input_str: str = None
) -> List[Task]:
    """Build a list of tasks from tasks data."""
    tasks = []
    for task_data in tasks_data:
        agent_id = task_data["agent_id"]
        if agent_id not in agents:
            raise ValueError(
                f"Agent with id {agent_id} not found for task {task_data['id']}."
            )
        tasks.append(create_task(task_data, agents[agent_id], input_str))
    return tasks


def extract_filtered_content(history: List) -> str:
    """Extract and filter content from chat history."""
    filtered_content = []
    for message in history:
        if isinstance(message, str):
            filtered_content.append(message)
        elif isinstance(message, dict):
            if message.get("role") == "assistant" and message.get("type") == "result":
                filtered_content.append(message.get("content", ""))
    return "\n".join(filtered_content)


def execute_crew(profile: Profile, crew_id: int, input_str: str) -> str:
    """Execute a crew synchronously."""
    tools_map = initialize_tools(profile)
    agents_data, tasks_data = fetch_crew_data(crew_id)

    agents = build_agents_dict(agents_data, tools_map)
    tasks = build_tasks_list(tasks_data, agents, input_str)
    crew = create_crew(list(agents.values()), tasks)

    return crew.kickoff(inputs={"user_input": input_str})


def build_single_crew(agents_data: List, tasks_data: List) -> Crew:
    """Build a single crew with the given agents and tasks data."""
    profile = Profile(account_index="", id=0)
    tools_map = initialize_tools(profile)
    agents = build_agents_dict(agents_data, tools_map)
    tasks = build_tasks_list(tasks_data, agents)
    return create_crew(list(agents.values()), tasks)


async def execute_chat_stream(profile: Profile, history: List, input_str: str):
    """Execute a chat stream with history."""
    callback_queue = asyncio.Queue()
    tools_map = initialize_tools(profile)
    filtered_content = extract_filtered_content(history)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    def chat_step_callback(step: Union[Dict[str, Any], AgentAction, AgentFinish]):
        if isinstance(step, AgentAction):
            asyncio.run_coroutine_threadsafe(
                callback_queue.put(
                    {
                        "type": "step",
                        "role": "assistant",
                        "content": step.thought,
                        "thought": step.thought,
                        "result": step.result,
                        "tool": step.tool,
                        "tool_input": step.tool_input,
                    }
                ),
                loop,
            )
        elif isinstance(step, AgentFinish):
            asyncio.run_coroutine_threadsafe(
                callback_queue.put(
                    {
                        "type": "step",
                        "role": "assistant",
                        "content": step.output,
                        "thought": step.thought,
                        "result": step.output,
                        "tool": None,
                        "tool_input": None,
                    }
                ),
                loop,
            )

    def chat_task_callback(task: TaskOutput):
        asyncio.run_coroutine_threadsafe(
            callback_queue.put(
                {"type": "task", "role": "assistant", "content": task.summary}
            ),
            loop,
        )

    chat_specialist = Agent(
        role="Chat Specialist",
        goal="You are responsible for interacting with the user and translating their query into an action.",
        backstory="You are trained to understand the user's query and provide the information they need with your tools, then analyzing the connection between the user's input and the result.",
        tools=tools_map.values(),
        verbose=AIBTC_CREWAI_VERBOSE,
        memory=False,
        allow_delegation=True,
    )

    task_description = dedent(
        f"""
        Review the user's input and chat history, then respond appropriately.
        
        Chat History:
        {filtered_content}
        
        Current Input:
        {input_str}
        
        Analyze the input and history, then provide a response that:
        1. Addresses the user's current query
        2. Takes into account any relevant context from the chat history
        3. Uses available tools when necessary to gather information or perform actions
        4. Maintains a consistent and helpful tone throughout the interaction
    """
    ).strip()

    review_user_input = Task(
        description=task_description,
        expected_output="A clear and contextually appropriate response to the user's input",
        agent=chat_specialist,
    )

    crew = create_crew(
        [chat_specialist],
        [review_user_input],
        step_callback=chat_step_callback,
        task_callback=chat_task_callback,
    )

    kickoff_future = loop.run_in_executor(None, crew.kickoff)

    while not kickoff_future.done():
        try:
            result = await asyncio.wait_for(callback_queue.get(), timeout=0.1)
            yield result
        except asyncio.TimeoutError:
            continue

    final_result = await kickoff_future
    while not callback_queue.empty():
        yield await callback_queue.get()
    yield {
        "type": "result",
        "content": final_result.raw,
        "tokens": final_result.token_usage.total_tokens,
    }


async def execute_crew_stream(profile: Profile, crew_id: UUID, input_str: str):
    """Execute a crew with streaming output."""
    callback_queue = asyncio.Queue()
    tools_map = initialize_tools(profile)

    try:
        agents_data, tasks_data = fetch_crew_data(crew_id)
    except ValueError as e:
        yield {"type": "result", "content": f"Error fetching crew data: {e}"}
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    def crew_step_callback(step: Union[Dict[str, Any], AgentAction, AgentFinish]):
        if isinstance(step, AgentAction):
            asyncio.run_coroutine_threadsafe(
                callback_queue.put(
                    {
                        "type": "step",
                        "role": "assistant",
                        "content": step.thought,
                        "thought": step.thought,
                        "result": step.result,
                        "tool": step.tool,
                        "tool_input": step.tool_input,
                    }
                ),
                loop,
            )
        elif isinstance(step, AgentFinish):
            asyncio.run_coroutine_threadsafe(
                callback_queue.put(
                    {
                        "type": "step",
                        "role": "assistant",
                        "content": step.output,
                        "thought": step.thought,
                        "result": step.output,
                        "tool": None,
                        "tool_input": None,
                    }
                ),
                loop,
            )

    def crew_task_callback(task: TaskOutput):
        asyncio.run_coroutine_threadsafe(
            callback_queue.put(
                {"type": "task", "role": "assistant", "content": task.summary}
            ),
            loop,
        )

    agents = build_agents_dict(agents_data, tools_map)
    tasks = build_tasks_list(tasks_data, agents, input_str)
    crew = create_crew(
        list(agents.values()),
        tasks,
        step_callback=crew_step_callback,
        task_callback=crew_task_callback,
    )

    kickoff_future = loop.run_in_executor(None, crew.kickoff, {"user_input": input_str})

    while not kickoff_future.done():
        try:
            result = await asyncio.wait_for(callback_queue.get(), timeout=0.1)
            yield result
        except asyncio.TimeoutError:
            continue

    final_result = await kickoff_future
    while not callback_queue.empty():
        yield await callback_queue.get()
    yield {
        "type": "result",
        "content": final_result.raw,
        "tokens": final_result.token_usage.total_tokens,
    }


async def execute_chat_stream_langgraph(
    profile: Profile, history: List, input_str: str
):
    """Execute a chat stream using LangGraph."""
    logger.debug("Starting execute_chat_stream_langgraph")
    callback_queue = asyncio.Queue()
    tools_map = initialize_tools(profile, crewai=False)  # Use LangChain tools
    filtered_content = extract_filtered_content(history)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    logger.debug(f"Converting history to messages, history length: {len(history)}")
    # Convert history to LangChain message format
    messages = []
    for msg in filtered_content:
        if msg["role"] == "user":
            messages.append(HumanMessage(content=msg["content"]))
        else:
            messages.append(AIMessage(content=msg["content"]))

    # Add the current input
    messages.append(HumanMessage(content=input_str))
    logger.debug(f"Final messages length: {len(messages)}")

    # Create the chat model with streaming
    callback_handler = StreamingCallbackHandler(
        queue=callback_queue,
        on_llm_new_token=lambda token, **kwargs: asyncio.run_coroutine_threadsafe(
            callback_queue.put({"type": "token", "content": token}), loop
        ),
        on_llm_end=lambda *args, **kwargs: asyncio.run_coroutine_threadsafe(
            callback_queue.put({"type": "end"}), loop
        ),
    )

    chat = ChatOpenAI(
        streaming=True,
        model="gpt-4o",
        callbacks=[callback_handler],
        temperature=0.7,  # Add some temperature for more natural responses
    )

    # Define the tool selection node
    async def tool_selection_node(state):
        logger.debug("Entering tool_selection_node")
        logger.debug(f"Current state messages: {len(state['messages'])}")

        try:
            if should_use_tool(state):
                logger.debug("Tool usage detected, initializing agent")
                # Convert tools list to LangChain format
                tools = list(tools_map.values())
                logger.debug(f"Using {len(tools)} tools")

                # Configure the agent with tools
                agent = initialize_agent(
                    tools=tools,
                    llm=chat,
                    agent=AgentType.OPENAI_FUNCTIONS,  # More flexible agent type
                    verbose=True,
                    handle_parsing_errors=True,
                    max_iterations=3,  # Limit iterations to prevent infinite loops
                    return_intermediate_steps=True,  # Get intermediate steps for streaming
                )

                try:
                    logger.debug("Running agent with tools")
                    logger.debug(
                        f"Last message: {state['messages'][-1].content if state['messages'] else ''}"
                    )
                    # Get the last message content
                    last_message = (
                        state["messages"][-1].content if state["messages"] else ""
                    )

                    # Run the agent with proper async handling
                    logger.debug("Invoking agent with callbacks")
                    agent_result = await agent.ainvoke(
                        {"input": last_message},
                        config={
                            "callbacks": [callback_handler],
                            "run_name": "tool_execution",
                        },
                    )
                    logger.debug(f"Agent result: {agent_result}")

                    # Process intermediate steps
                    for step in agent_result.get("intermediate_steps", []):
                        action, output = step
                        logger.debug(
                            f"Processing step - action: {action}, output: {output}"
                        )
                        logger.debug(f"Action tool: {action.tool}")
                        logger.debug(f"Action tool_input: {action.tool_input}")

                        # Convert tool input to string if it's a dict
                        tool_input = action.tool_input
                        if isinstance(tool_input, dict):
                            tool_input = str(tool_input)

                        tool_execution = {
                            "type": "tool_execution",
                            "tool": action.tool,
                            "input": tool_input,
                            "output": str(output),  # Ensure output is string
                        }
                        logger.debug(f"Sending tool execution: {tool_execution}")
                        await callback_queue.put(tool_execution)
                        logger.debug(f"Processed intermediate step: {tool_execution}")

                    # Get final response
                    response_content = agent_result.get("output", "")
                    logger.debug(
                        f"Agent response received: {response_content[:100]}..."
                    )

                    return {
                        "messages": state["messages"]
                        + [AIMessage(content=response_content)],
                        "response": response_content,
                    }
                except Exception as e:
                    logger.error(f"Error in tool execution: {str(e)}")
                    # Fallback to regular chat if tool execution fails
                    logger.debug("Falling back to regular chat after tool error")

            # Use regular chat without tools (either by choice or after tool failure)
            logger.debug("Using regular chat without tools")
            messages = state["messages"]
            response = await chat.ainvoke(messages)
            response_content = (
                response.content if hasattr(response, "content") else str(response)
            )
            logger.debug(f"Chat response received: {response_content[:100]}...")
            return {
                "messages": messages + [AIMessage(content=response_content)],
                "response": response_content,
            }
        except Exception as e:
            logger.error(f"Error in tool_selection_node: {str(e)}")
            raise

    # Create the graph
    logger.debug("Creating workflow graph")
    workflow = Graph()
    workflow.add_node("chat", tool_selection_node)
    workflow.set_entry_point("chat")
    workflow.add_edge("chat", END)

    # Compile the graph into a runnable
    logger.debug("Compiling workflow")
    runnable = workflow.compile()

    # Run the graph
    config = {"messages": messages}
    logger.debug("Starting workflow execution")
    task = asyncio.create_task(runnable.ainvoke(config))

    # Stream tokens while waiting for completion
    while not task.done():
        try:
            logger.debug("Waiting for data from callback queue")
            try:
                # Try to get data with a timeout to avoid getting stuck
                data = await asyncio.wait_for(callback_queue.get(), timeout=0.1)
                logger.debug(f"Received data from queue: {data}")
                if data["type"] == "end":
                    yield data
                else:
                    yield data
            except asyncio.TimeoutError:
                # No data available, continue checking if task is done
                continue
        except asyncio.CancelledError:
            logger.error("Task cancelled")
            task.cancel()
            raise
        except Exception as e:
            logger.error(f"Error in streaming loop: {str(e)}")
            raise

    # Get final result
    try:
        logger.debug("Getting final result")
        result = await task
        logger.debug(f"Final result received: {result}")
    except Exception as e:
        logger.error(f"Error getting final result: {str(e)}")
        raise

    yield {
        "type": "result",
        "content": result["response"],
        "tokens": None,  # LangGraph doesn't provide token count directly
    }


class StreamingCallbackHandler(BaseCallbackHandler):
    """Callback handler for streaming tokens."""

    def __init__(self, queue: asyncio.Queue, on_llm_new_token=None, on_llm_end=None):
        """Initialize the callback handler with a queue and optional callbacks."""
        super().__init__()
        self.queue = queue
        self._on_llm_new_token = on_llm_new_token
        self._on_llm_end = on_llm_end
        self.tokens = []
        self.current_tool = None
        self._loop = None

    def _ensure_loop(self):
        """Ensure we have a valid event loop."""
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        return self._loop

    def _put_to_queue(self, item):
        """Helper method to put items in queue."""
        loop = self._ensure_loop()
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.queue.put(item), loop)
            future.result()  # Wait for it to complete
        else:
            loop.run_until_complete(self.queue.put(item))

    def on_llm_start(self, *args, **kwargs):
        """Run when LLM starts running."""
        logger.debug("LLM started")

    def on_llm_new_token(self, token: str, **kwargs):
        """Run on new token. Only available when streaming is enabled."""
        if self._on_llm_new_token:
            self._on_llm_new_token(token, **kwargs)
        self.tokens.append(token)

    def on_llm_end(self, response: LLMResult, **kwargs):
        """Run when LLM ends running."""
        logger.debug("LLM ended")
        if self._on_llm_end:
            self._on_llm_end(response, **kwargs)

    def on_llm_error(self, error: Exception, **kwargs):
        """Run when LLM errors."""
        logger.error(f"LLM error: {str(error)}")

    # def on_tool_start(self, serialized: Dict, input_str: str, **kwargs):
    #     """Run when tool starts running."""
    #     self.current_tool = serialized.get("name")
    #     tool_execution = {
    #         "type": "tool_execution",
    #         "tool": self.current_tool,
    #         "input": input_str,
    #         "output": None,
    #     }
    #     self._put_to_queue(tool_execution)

    # def on_tool_end(self, output: str, **kwargs):
    #     """Run when tool ends running."""
    #     if self.current_tool:
    #         tool_execution = {
    #             "type": "tool_execution",
    #             "tool": self.current_tool,
    #             "input": None,  # We don't have access to the input here
    #             "output": output,
    #         }
    #         self._put_to_queue(tool_execution)
    #         self.current_tool = None

    def on_tool_error(self, error: Exception, **kwargs):
        """Run when tool errors."""
        if self.current_tool:
            tool_execution = {
                "type": "tool_execution",
                "tool": self.current_tool,
                "input": None,  # We don't have access to the input here
                "output": f"Error: {str(error)}",
            }
            self._put_to_queue(tool_execution)
            self.current_tool = None
        logger.error(f"Tool error: {str(error)}")


def should_use_tool(state):
    """Determine if a tool should be used based on the current state."""
    messages = state["messages"]
    last_message = messages[-1].content if messages else ""

    # Add your conditional logic here
    # Example conditions:
    if any(
        keyword in last_message.lower()
        for keyword in ["price", "balance", "transaction", "wallet"]
    ):
        return True
    if "get" in last_message.lower() and any(
        resource in last_message.lower()
        for resource in ["token", "contract", "address"]
    ):
        return True
    return False
