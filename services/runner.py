import os
from backend.factory import backend
from backend.models import (
    DAOBase,
    DAOFilter,
    Profile,
    QueueMessageBase,
    QueueMessageFilter,
    TokenFilter,
    WalletFilter,
    XTweetFilter,
    XUserBase,
)
from datetime import datetime
from lib.logger import configure_logger
from services.langgraph import execute_langgraph_stream
from services.tweet_generator import generate_dao_tweet
from services.twitter import TweetData, TwitterMentionHandler
from tools.tools_factory import filter_tools_by_names, initialize_tools
from uuid import UUID

logger = configure_logger(__name__)


class TweetRunner:
    """Handles processing of queued tweet responses."""

    def __init__(self):
        """Initialize the Twitter handler."""
        twitter_profile_id = os.getenv("AIBTC_TWITTER_PROFILE_ID")
        if not twitter_profile_id:
            raise ValueError("AIBTC_TWITTER_PROFILE_ID environment variable is not set")
        self.twitter_profile_id = UUID(twitter_profile_id)

        twitter_agent_id = os.getenv("AIBTC_TWITTER_AGENT_ID")
        if not twitter_agent_id:
            raise ValueError("AIBTC_TWITTER_AGENT_ID environment variable is not set")
        self.twitter_agent_id = UUID(twitter_agent_id)
        twitter_wallet = backend.list_wallets(
            filters=WalletFilter(profile_id=self.twitter_profile_id)
        )
        if not twitter_wallet:
            logger.error("No Twitter wallet found")
            return

        self.twitter_wallet_id = twitter_wallet[0].id
        self.twitter_handler = TwitterMentionHandler()

    async def run(self) -> None:
        """Process tweet responses from queue."""
        try:
            # Get unprocessed tweet messages from queue
            queue_messages = backend.list_queue_messages(
                filters=QueueMessageFilter(type="tweet", is_processed=False)
            )
            if not queue_messages:
                logger.info("No tweet messages in queue")
                return

            ## Get all DAO messages that are processed
            dao_messages = backend.list_queue_messages(
                filters=QueueMessageFilter(type="daos", is_processed=True)
            )
            logger.info(f"Found {len(dao_messages)} processed DAO messages")

            for message in queue_messages:
                logger.info(f"Processing tweet message: {message}")
                try:
                    if not message.dao_id:
                        logger.error(f"Tweet message {message.id} has no dao_id")
                        continue

                    # Get the DAO and token info for this tweet message
                    dao = backend.get_dao(message.dao_id)
                    if not dao:
                        logger.error(f"No DAO found for id: {message.dao_id}")
                        continue

                    token = backend.list_tokens(
                        filters=TokenFilter(dao_id=message.dao_id)
                    )
                    if not token or len(token) == 0:
                        logger.error(f"No token found for DAO: {message.dao_id}")
                        continue

                    # Find matching DAO message by comparing token details
                    matching_dao_message = None
                    for dao_message in dao_messages:
                        if not isinstance(dao_message.message, dict):
                            continue

                        params = dao_message.message.get("parameters", {})
                        if (
                            params.get("token_symbol") == token[0].symbol
                            and params.get("token_name") == token[0].name
                            and params.get("token_max_supply") == token[0].max_supply
                        ):
                            matching_dao_message = dao_message
                            logger.info(
                                f"Found matching DAO message: {dao_message.id} for token {token[0].symbol}"
                            )
                            break

                    if not matching_dao_message:
                        logger.error(
                            f"No matching DAO message found for dao_id: {message.dao_id} "
                            f"with token symbol: {token[0].symbol}, name: {token[0].name}"
                        )
                        backend.update_queue_message(
                            queue_message_id=message.id,
                            update_data=QueueMessageBase(is_processed=True),
                        )
                        continue

                    # Generate an exciting tweet about the DAO deployment
                    generated_tweet = await generate_dao_tweet(
                        dao_name=dao.name,
                        dao_symbol=token[0].symbol,
                        dao_mission=dao.mission,
                        dao_id=dao.id,
                    )
                    response_content = generated_tweet["tweet_text"]

                    # Post the response using the matching DAO message details
                    logger.info(
                        f"Posting response for tweet_id: {matching_dao_message.tweet_id}, "
                        f"conversation_id: {matching_dao_message.conversation_id}"
                    )
                    await self.twitter_handler._post_response(
                        tweet_data=TweetData(
                            tweet_id=matching_dao_message.tweet_id,
                            conversation_id=matching_dao_message.conversation_id,
                        ),
                        response_content=response_content,
                    )

                    tweet_info = backend.list_x_tweets(
                        filters=XTweetFilter(tweet_id=matching_dao_message.tweet_id)
                    )
                    if not tweet_info or len(tweet_info) == 0:
                        logger.error(
                            f"No tweet info found for tweet_id: {matching_dao_message.tweet_id}"
                        )
                        continue

                    author_id = tweet_info[0].author_id

                    backend.update_dao(
                        dao_id=dao.id,
                        update_data=DAOBase(
                            author_id=author_id,
                        ),
                    )
                    user = (
                        await self.twitter_handler.twitter_service.get_user_by_user_id(
                            matching_dao_message.author_id
                        )
                    )
                    if user:
                        logger.info(f"User: {user}")
                        # update user info in db
                        backend.update_x_user(
                            x_user_id=user.id,
                            update_data=XUserBase(
                                name=user.name,
                                username=user.username,
                                description=user.description,
                                location=user.location,
                                profile_image_url=user.profile_image_url,
                                profile_banner_url=user.profile_banner_url,
                                protected=user.protected,
                                verified=user.verified,
                                verified_type=user.verified_type,
                                subscription_type=user.subscription_type,
                            ),
                        )

                    # Mark message as processed only after successful posting
                    backend.update_queue_message(
                        queue_message_id=message.id,
                        update_data=QueueMessageBase(is_processed=True),
                    )
                    logger.info(f"Successfully processed tweet message {message.id}")

                except Exception as e:
                    logger.error(
                        f"Error processing tweet message {message.id} for DAO {message.dao_id}: {str(e)}",
                        exc_info=True,
                    )
                    continue

        except Exception as e:
            logger.error(f"Error in tweet runner: {str(e)}", exc_info=True)
            raise


class DAORunner:
    """Handles processing of queued DAO deployment requests."""

    def __init__(self):
        """Initialize the runner with necessary tools."""
        twitter_profile_id = os.getenv("AIBTC_TWITTER_PROFILE_ID")
        if not twitter_profile_id:
            raise ValueError("AIBTC_TWITTER_PROFILE_ID environment variable is not set")
        self.twitter_profile_id = UUID(twitter_profile_id)

        twitter_agent_id = os.getenv("AIBTC_TWITTER_AGENT_ID")
        if not twitter_agent_id:
            raise ValueError("AIBTC_TWITTER_AGENT_ID environment variable is not set")
        self.twitter_agent_id = UUID(twitter_agent_id)
        self.tools_map_all = initialize_tools(
            Profile(
                id=self.twitter_profile_id,
                created_at=datetime.now(),
            ),
            agent_id=self.twitter_agent_id,
        )
        self.tools_map = filter_tools_by_names(
            ["contract_dao_deploy"], self.tools_map_all
        )
        twitter_wallet = backend.list_wallets(
            filters=WalletFilter(profile_id=self.twitter_profile_id)
        )
        if not twitter_wallet:
            logger.error("No Twitter wallet found")
            return

        self.twitter_wallet_id = twitter_wallet[0].id

        logger.info(f"Initialized tools_map with {len(self.tools_map)} tools")

    async def run(self) -> None:
        """Process DAO deployments and queue."""
        try:

            # Check for any pending Twitter DAOs
            pending_daos = backend.list_daos(
                filters=DAOFilter(
                    is_deployed=False,
                    is_broadcasted=True,
                    wallet_id=self.twitter_wallet_id,
                )
            )

            if pending_daos:
                logger.info("Found pending Twitter DAO, skipping queue processing")
                return

            # No pending DAOs, process next from queue
            queue_messages = backend.list_queue_messages(
                filters=QueueMessageFilter(type="daos", is_processed=False)
            )
            if not queue_messages:
                logger.info("No messages in queue")
                return

            message = queue_messages[0]
            logger.info(f"Processing message: {message}")
            backend.update_queue_message(
                queue_message_id=message.id,
                update_data=QueueMessageBase(is_processed=True),
            )
            # Construct the input for the tool
            tool_input = (
                f"Please deploy a DAO with the following parameters:\n"
                f"Token Symbol: {message.message['parameters']['token_symbol']}\n"
                f"Token Name: {message.message['parameters']['token_name']}\n"
                f"Token Description: {message.message['parameters']['token_description']}\n"
                f"Token Max Supply: {message.message['parameters']['token_max_supply']}\n"
                f"Token Decimals: {message.message['parameters']['token_decimals']}\n"
                f"Mission: {message.message['parameters']['mission']}"
            )

            # Execute using langgraph
            async for chunk in execute_langgraph_stream(
                history=[], input_str=tool_input, tools_map=self.tools_map
            ):
                if chunk["type"] == "result":
                    logger.info(f"DAO deployment completed: {chunk['content']}")
                    # Mark message as processed after successful deployment

                elif chunk["type"] == "tool":
                    logger.info(f"Tool execution: {chunk}")

        except Exception as e:
            logger.error(f"Error in runner: {str(e)}")
            raise


# Global runner instances
dao_runner = DAORunner()
tweet_runner = TweetRunner()


async def execute_runner_job(runner_type: str = "dao") -> None:
    """Execute the runner jobs to process DAO deployments and tweets.

    Args:
        runner_type: The type of runner to execute ("dao" or "tweet")
    """
    try:
        if runner_type == "dao":
            logger.info("Starting DAO runner")
            await dao_runner.run()
            logger.info("Completed DAO runner")
        elif runner_type == "tweet":
            logger.info("Starting Tweet runner")
            await tweet_runner.run()
            logger.info("Completed Tweet runner")
        else:
            logger.error(f"Unknown runner type: {runner_type}")
            raise ValueError(f"Unknown runner type: {runner_type}")

    except Exception as e:
        logger.error(f"Error in runner job: {str(e)}")
        raise
