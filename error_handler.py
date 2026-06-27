import html
import os
import traceback

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))


async def error_handler(update, context):
    # Get full traceback
    tb = "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__
    ))

    # Build message
    message = (
        f"⚠️ <b>Bot Error</b>\n\n"
        f"<b>Error:</b> {html.escape(str(context.error))}\n\n"
        f"<b>Traceback:</b>\n<pre>{html.escape(tb[-3000:])}</pre>"
    )

    # Add update info if available
    if update:
        message += f"\n\n<b>Update:</b> <pre>{html.escape(str(update))[:500]}</pre>"

    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=message,
        parse_mode="HTML"
    )
