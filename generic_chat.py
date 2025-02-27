import time
import logging
import openai
import discord
import aiohttp
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from status_utils import update_status
from message_utils import delete_msg

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "o3-mini"
DEFAULT_REPLY_FOOTER = "o3-mini | default"

async def process_attachments(prompt: str, attachments: list, is_slash: bool = False) -> (str, str):
    image_url = None
    final_prompt = prompt
    if attachments:
        for att in attachments:
            filename = att.filename.lower()
            if filename.endswith(".txt"):
                try:
                    username_prefix = ""
                    if prompt.startswith("Message from "):
                        parts = prompt.split(":", 1)
                        if len(parts) > 1:
                            username_prefix = parts[0] + ": "
                    if is_slash:
                        file_bytes = await att.read()
                        final_prompt = username_prefix + file_bytes.decode("utf-8")
                    else:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(att.url) as response:
                                if response.status == 200:
                                    final_prompt = await response.text()
                                else:
                                    logger.warning(f"Failed to download attachment: {att.url} with status {response.status}")
                except Exception as e:
                    logger.exception("Error processing text attachment: %s", e)
            elif filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")) and not image_url:
                image_url = att.proxy_url or att.url
    return final_prompt, image_url


async def get_reference_message(ctx) -> str:
    reference_message = ""
    if hasattr(ctx, "message") and ctx.message.reference:
        try:
            if ctx.message.reference.cached_message:
                ref_msg = ctx.message.reference.cached_message
            else:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            
            if ref_msg.author.id == ctx.bot.user.id:
                if ref_msg.embeds and ref_msg.embeds[0].description:
                    reference_message = ref_msg.embeds[0].description.strip()
            else:
                reference_message = f"Message from {ref_msg.author.name}: {ref_msg.content}"
        except Exception as e:
            logger.exception("Failed to fetch reference message: %s", e)
    return reference_message


async def prepare_chat_parameters(prompt: str, attachments: list = None, ctx=None, is_slash: bool = False) -> (str, str, str, str, str, str):
    final_prompt, image_url = await process_attachments(prompt, attachments or [], is_slash=is_slash)

    model, reply_footer = DEFAULT_MODEL, DEFAULT_REPLY_FOOTER
    
    reference_message = ""
    if not is_slash and ctx is not None:
        reference_message = await get_reference_message(ctx)
        
    return final_prompt, image_url, model, reply_footer, reference_message

async def perform_chat_query(
    prompt: str,
    api_cog,
    channel: discord.TextChannel,
    duck_cog=None,
    image_url: str = None,
    reference_message: str = None,
    model: str = DEFAULT_MODEL,
    reply_footer: str = DEFAULT_REPLY_FOOTER,
    show_status: bool = True,
    api: str = "openai",
) -> (str, float, str):
    start_time = time.time()
    original_prompt = prompt
    
    if not prompt.startswith("Message from"):
        logger.warning("Prompt missing username prefix, this should be handled by the caller")
    
    if duck_cog is not None:
        try:
            ddg_summary = await duck_cog.search_and_summarize(original_prompt)
        except Exception as e:
            logger.exception("Error during DuckDuckGo search: %s", e)
            ddg_summary = None
        if ddg_summary:
            prompt = original_prompt + "\n\nSummary of Relevant Web Search Results:\n" + ddg_summary

    status_msg = None
    if show_status:
        status_msg = await update_status(None, "...generating reply...", channel=channel)
        
    try:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((openai.APIError, openai.APIConnectionError, openai.RateLimitError)),
            wait=wait_exponential(min=1, max=10),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                result = await api_cog.send_request(
                    model=model,
                    message_content=prompt,
                    reference_message=reference_message,
                    image_url=image_url,
                    api=api,
                )
                break
        elapsed = round(time.time() - start_time, 2)
        
        if status_msg:
            await delete_msg(status_msg)
            
        return result, elapsed, reply_footer
    except Exception as e:
        if status_msg:
            await delete_msg(status_msg)
        logger.exception("Error in perform_chat_query: %s", e)
        raise