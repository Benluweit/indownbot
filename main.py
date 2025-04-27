import sqlite3
import os
import random
import hashlib
import asyncio
import re
from dotenv import load_dotenv
from datetime import datetime, time
from translation import get_translation
import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import logging
from sqlite3 import OperationalError

MAX_RETRIES = 3
RETRY_DELAY = 0.1  # seconds
# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
ENTRY_PRICE = 1.0  # USD equivalent
DAILY_DRAW_TIME = "20:00"  # 8:00 PM UTC

# Prize Multipliers
PRIZES = {
    3: 5,    # 5x for 3 correct numbers
    4: 50,   # 50x for 4 correct numbers
    5: 500   # 500x for 5 correct numbers
}

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.info("Logger initialized.")

# Database setup
def initialize_db():
    with sqlite3.connect('lottery.db') as conn:
        # Enable WAL mode for better concurrency
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        cursor = conn.cursor()

        # Create tables if they don't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                tg_name TEXT,
                language TEXT DEFAULT 'en',
                user_balance REAL DEFAULT 0,
                referral_code TEXT UNIQUE
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                first_deposit_bonus BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_id) REFERENCES users(user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tg_name TEXT,
                ticket_code TEXT,
                stake_amount REAL,
                user_input TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS deposits (
                dp_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                tg_name TEXT,
                dp_meth TEXT,
                tx_hash TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                wd_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                tg_name TEXT,
                wd_type TEXT,
                wd_amt REAL,
                address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS draws (
                draw_id INTEGER PRIMARY KEY AUTOINCREMENT,
                draw TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS winners (
                winner_id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_id INTEGER,
                user_id INTEGER,
                tg_name TEXT,
                ticket_code TEXT,
                stake_amount REAL,
                win_amount REAL,
                user_input TEXT,
                stake_time DATETIME,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS commissions (
                comm_id INTEGER PRIMARY KEY AUTOINCREMENT,
                commissions TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()

def add_user(user_id, user_name, tg_name):
    with sqlite3.connect('lottery.db') as conn:
        cursor = conn.cursor()
        # First check if user exists
        cursor.execute('SELECT referral_code FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if not result:  # User doesn't exist, create with referral code
            referral_code = hashlib.md5(f"{user_id}{user_name}{datetime.now()}".encode()).hexdigest()[:8]
            cursor.execute('''
                INSERT INTO users (user_id, user_name, tg_name, user_balance, referral_code)
                VALUES (?, ?, ?, 0, ?)
            ''', (user_id, user_name, tg_name, referral_code))
            conn.commit()
            return referral_code
        return result[0]  # Return existing referral code

# Command handler for /admin when Admin sends command
async def adminstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.message.from_user
        user_id = user.id
        
        # Check if ADMIN_ID is set and if the user is the admin
        if ADMIN_ID and user_id == ADMIN_ID:
            user_name = user.username or user.first_name
            tg_name = user.first_name
            add_user(user_id, user_name, tg_name)

            # Calculate commissions and total balance
            with sqlite3.connect('lottery.db') as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT SUM(commissions) FROM commissions')
                total_commissions = cursor.fetchone()[0] or 0
                
                cursor.execute('SELECT SUM(user_balance) FROM users')
                total_balance = cursor.fetchone()[0] or 0

            keyboard = [
                [InlineKeyboardButton("Deposits", callback_data='confirmdeposit')],
                [InlineKeyboardButton("Withdrawals", callback_data='confirmwithdrawals')],
                [InlineKeyboardButton("Enter Game", callback_data='entergame')],
                [InlineKeyboardButton("Terms & Conditions", url='https://t.me/m345_support')],
                [InlineKeyboardButton("Customer Service", url='https://t.me/m345_support')]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            start_message = f"Commissions: ${total_commissions:.2f}\nNet available: ${total_balance:.2f}"
            await update.message.reply_text(start_message, reply_markup=reply_markup)
        else:
            await update.message.reply_text("You don't have admin privileges.")
    except Exception as e:
        logger.error(f"Error in adminstart command: {e}")
        await update.message.reply_text("An error occurred. Please try again later.")

# Command handler for /start - handles both admin and non-admin cases
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user = update.message.from_user
        user_id = user.id
        user_name = user.username or user.first_name
        tg_name = user.first_name

        # Add user and get referral code
        referral_code = add_user(user_id, user_name, tg_name)

        # Check if coming from referral link
        if context.args and len(context.args) == 1:
            referral_code_from_link = context.args[0]
            await handle_referral(context, user_id, referral_code_from_link)

        # Get user's language
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT language FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            current_lang = result[0] if result and result[0] else 'en'
            context.user_data['language'] = current_lang

        # Generate referral link
        bot_username = (await context.bot.get_me()).username
        referral_link = f"https://t.me/share/url?url=https://t.me/{bot_username}?start={referral_code}&text=Predict%205%20numbers%20and%20stand%20a%20chance%20of%20winning%20if%20you%20match%203,%204%20or%205%20numbers%20with%20the%20daily%20draw%20at%208PM"

        # Check if user is admin
        if ADMIN_ID and user_id == ADMIN_ID:
            # Admin keyboard
            keyboard = [
                [InlineKeyboardButton("Deposits", callback_data='confirmdeposit')],
                [InlineKeyboardButton("Withdrawals", callback_data='confirmwithdrawals')],
                [InlineKeyboardButton("Enter Game", callback_data='entergame')],
                [InlineKeyboardButton("Terms & Conditions", url='https://t.me/m345_support')],
                [InlineKeyboardButton("Customer Service", url='https://t.me/m345_support')]
            ]
            start_message = get_translation('welcome_admin', current_lang)
        else:
            # Regular user keyboard
            keyboard = [
                [InlineKeyboardButton(get_translation('enter_game', current_lang), callback_data='entergame')],
                [InlineKeyboardButton(get_translation('change_language', current_lang), callback_data='languages')],
                [InlineKeyboardButton(get_translation('refer', current_lang), url=referral_link)],
                [InlineKeyboardButton(get_translation('terms_conditions', current_lang), url='https://t.me/m345_support')],
                [InlineKeyboardButton(get_translation('customer_service', current_lang), url='https://t.me/m345_support')]
            ]
            start_message = get_translation('welcome_user', current_lang)  # Changed from 'welcome_admin'

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(start_message, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text("An error occurred. Please try again later.")

async def handle_referral(context: ContextTypes.DEFAULT_TYPE, referred_id: int, referral_code: str):
    """Record referral relationship only for first-time users"""
    with sqlite3.connect('lottery.db') as conn:
        cursor = conn.cursor()
        
        # Check if user already exists and has any deposits
        cursor.execute('''
            SELECT COUNT(*) FROM deposits WHERE user_id = ?
        ''', (referred_id,))
        has_deposits = cursor.fetchone()[0] > 0
        
        if has_deposits:
            logger.info(f"User {referred_id} already has deposits, skipping referral")
            return
            
        # Check if user already has a referral record
        cursor.execute('''
            SELECT COUNT(*) FROM referrals WHERE referred_id = ?
        ''', (referred_id,))
        has_referral = cursor.fetchone()[0] > 0
        
        if has_referral:
            logger.info(f"User {referred_id} already has a referral record")
            return
            
        # Get referrer_id from referral_code
        cursor.execute('SELECT user_id FROM users WHERE referral_code = ?', (referral_code,))
        result = cursor.fetchone()
        
        if result:
            referrer_id = result[0]
            # Prevent self-referral
            if referrer_id != referred_id:
                # Record the referral
                cursor.execute('''
                    INSERT INTO referrals (referrer_id, referred_id)
                    VALUES (?, ?)
                ''', (referrer_id, referred_id))
                conn.commit()
                
                # Notify referrer
                try:
                    referred_name = (await context.bot.get_chat(referred_id)).first_name
                    await context.bot.send_message(
                        chat_id=referrer_id,
                        text=f"👋 {referred_name} joined using your referral link!"
                    )
                except Exception as e:
                    logger.error(f"Could not notify referrer {referrer_id}: {e}")

async def credit_referrer_bonus(context: ContextTypes.DEFAULT_TYPE, user_id: int, amount: float):
    """Credit referrer with 10% of first deposit"""
    for attempt in range(MAX_RETRIES):
        try:
            with sqlite3.connect('lottery.db') as conn:
                cursor = conn.cursor()
                conn.execute('BEGIN IMMEDIATE TRANSACTION')
                
                # Check if this is user's first approved deposit
                cursor.execute('''
                    SELECT COUNT(*) FROM deposits 
                    WHERE user_id = ? AND dp_id NOT IN (
                        SELECT dp_id FROM deposits WHERE user_id = ? 
                        ORDER BY created_at LIMIT 1
                    )
                ''', (user_id, user_id))
                has_previous_deposits = cursor.fetchone()[0] > 0
                
                if has_previous_deposits:
                    conn.commit()
                    return
                    
                # Check if user has a referrer who hasn't received bonus yet
                cursor.execute('''
                    SELECT r.referrer_id 
                    FROM referrals r
                    WHERE r.referred_id = ? 
                    AND r.first_deposit_bonus = FALSE
                ''', (user_id,))
                result = cursor.fetchone()
                
                if result:
                    referrer_id = result[0]
                    bonus = amount * 0.10
                    
                    # Update referrer's balance
                    cursor.execute('''
                        UPDATE users 
                        SET user_balance = user_balance + ?
                        WHERE user_id = ?
                    ''', (bonus, referrer_id))
                    
                    # Mark bonus as credited
                    cursor.execute('''
                        UPDATE referrals
                        SET first_deposit_bonus = TRUE
                        WHERE referred_id = ? AND referrer_id = ?
                    ''', (user_id, referrer_id))
                    
                    conn.commit()
                    
                    # Notify referrer
                    try:
                        user_name = (await context.bot.get_chat(user_id)).first_name
                        notify_msg = await context.bot.send_message(
                            chat_id=referrer_id,
                            text=f"🎉 You received a ${bonus:.2f} referral bonus from {user_name}'s first deposit!"
                        )
                        # Delete notification after 10 seconds
                        asyncio.create_task(delete_after_delay(
                            context, 
                            referrer_id, 
                            notify_msg.message_id, 
                            10
                        ))
                    except Exception as e:
                        logger.error(f"Could not notify referrer {referrer_id}: {e}")
                else:
                    conn.commit()
                break  # Success, exit retry loop
        except OperationalError as e:
            if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                continue
            logger.error(f"Failed to credit referrer bonus after {MAX_RETRIES} attempts: {e}")
            break
        except Exception as e:
            logger.error(f"Unexpected error in credit_referrer_bonus: {e}")
            break

async def confirmdeposit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        
        current_lang = context.user_data.get('language', 'en')
        
        # Get current offset from user_data or initialize to 0
        offset = context.user_data.get('deposit_offset', 0)
        
        # Handle pagination actions
        if query.data == 'deposit_next':
            offset += 10
        elif query.data == 'deposit_prev':
            offset = max(0, offset - 10)
        
        # Save the current offset
        context.user_data['deposit_offset'] = offset
        # Activate deposit list
        context.user_data['deposit_list_active'] = True
        
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            
            # Get total count of deposits for pagination info
            cursor.execute('SELECT COUNT(*) FROM deposits')
            total_deposits = cursor.fetchone()[0]
            
            # Get paginated deposits
            cursor.execute('''
                SELECT dp_id, tg_name, dp_meth, tx_hash, created_at 
                FROM deposits 
                ORDER BY dp_id ASC 
                LIMIT 10 OFFSET ?
            ''', (offset,))
            deposits = cursor.fetchall()

        if not deposits:
            message = get_translation('no_pending_deposits', current_lang)
        else:
            range_start = offset + 1
            range_end = min(offset + 10, total_deposits)
            message = get_translation('deposits_header', current_lang).format(
                range_start=range_start,
                range_end=range_end,
                total=total_deposits
            ) + "\n\n"
            
            for deposit in deposits:
                dp_id, tg_name, dp_meth, tx_hash, created_at = deposit
                message += (
                    f"{get_translation('id_label', current_lang)}: {dp_id}\n"
                    f"{get_translation('user_label', current_lang)}: {tg_name}\n"
                    f"{get_translation('method_label', current_lang)}: {dp_meth}\n"
                    f"{get_translation('tx_hash_label', current_lang)}: {tx_hash}\n"
                    f"{get_translation('date_label', current_lang)}: {created_at}\n\n"
                    f"{get_translation('approve_command', current_lang)}: /approve_{dp_id}_AMOUNT\n"
                    f"{get_translation('reject_command', current_lang)}: /reject_{dp_id}\n"
                    f"{get_translation('separator', current_lang)}\n"
                )

        keyboard = []
        # Add pagination buttons only if needed
        if total_deposits > 10:
            keyboard.append([
                InlineKeyboardButton(
                    get_translation('prev_button', current_lang), 
                    callback_data='deposit_prev'
                ) if offset > 0 else InlineKeyboardButton(" ", callback_data='noop'),
                InlineKeyboardButton(
                    get_translation('next_button', current_lang), 
                    callback_data='deposit_next'
                ) if offset + 10 < total_deposits else InlineKeyboardButton(" ", callback_data='noop')
            ])
        keyboard.append([
            InlineKeyboardButton(
                get_translation('back_button', current_lang), 
                callback_data='back_to_adminstart'
            )
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Error in confirmdeposit_message: {e}")
        error_msg = get_translation('error_loading_deposits', context.user_data.get('language', 'en'))
        await query.edit_message_text(error_msg)

async def handle_deposit_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        
        if not context.user_data.get('deposit_list_active', False) or update.message.from_user.id != ADMIN_ID:
            await update.message.delete()
            return

        # Delete admin input after 3 seconds
        asyncio.create_task(delete_after_delay(context, update.message.chat_id, update.message.message_id, 3))

        command = update.message.text.strip().lower()
        parts = command.split()
        
        if len(parts) < 2:
            error_msg = await update.message.reply_text(
                get_translation('invalid_command_format', current_lang) + "\n" +
                get_translation('usage_examples', current_lang)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return
            
        action = parts[0]
        dp_id = parts[1]
        
        if action not in ['approve', 'reject']:
            error_msg = await update.message.reply_text(
                get_translation('invalid_action', current_lang)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return
            
        try:
            dp_id = int(dp_id)
        except ValueError:
            error_msg = await update.message.reply_text(
                get_translation('invalid_deposit_id', current_lang)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return

        # Get deposit details first (read operation, less likely to conflict)
        deposit = None
        for attempt in range(MAX_RETRIES):
            try:
                with sqlite3.connect('lottery.db') as conn:
                    cursor = conn.cursor()
                    cursor.execute('''
                        SELECT user_id, tg_name, dp_meth, tx_hash 
                        FROM deposits 
                        WHERE dp_id = ?
                    ''', (dp_id,))
                    deposit = cursor.fetchone()
                break
            except OperationalError as e:
                if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                raise

        if not deposit:
            error_msg = await update.message.reply_text(
                get_translation('deposit_not_found', current_lang).format(dp_id=dp_id)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return

        user_id, tg_name, method, tx_hash = deposit

        if action == 'approve':
            if len(parts) != 3:
                error_msg = await update.message.reply_text(
                    get_translation('approve_format', current_lang)
                )
                asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
                return
                
            try:
                amount = float(parts[2])
                if amount <= 0:
                    raise ValueError(get_translation('amount_positive', current_lang))
            except ValueError:
                error_msg = await update.message.reply_text(
                    get_translation('invalid_amount', current_lang)
                )
                asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
                return

            # Execute the approval transaction with retries
            for attempt in range(MAX_RETRIES):
                try:
                    with sqlite3.connect('lottery.db') as conn:
                        cursor = conn.cursor()
                        conn.execute('BEGIN IMMEDIATE TRANSACTION')

                        # Update user balance
                        cursor.execute('''
                            UPDATE users 
                            SET user_balance = user_balance + ? 
                            WHERE user_id = ?
                        ''', (amount, user_id))

                        # Record commission (10% of deposit)
                        commission = amount * 0.1
                        cursor.execute('''
                            INSERT INTO commissions (commissions) 
                            VALUES (?)
                        ''', (commission,))

                        # Delete the deposit record
                        cursor.execute('''
                            DELETE FROM deposits 
                            WHERE dp_id = ?
                        ''', (dp_id,))

                        conn.commit()
                        
                        # Credit referrer bonus AFTER successful deposit approval
                        await credit_referrer_bonus(context, user_id, amount)
                        
                        break  # Success, exit retry loop
                except OperationalError as e:
                    if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    raise

            # Create notification with OK button
            keyboard = [[InlineKeyboardButton(
                get_translation('ok_button', current_lang),
                callback_data=f'delete_notify_{update.message.message_id}'
            )]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            notify_msg = await update.message.reply_text(
                get_translation('deposit_approved_admin', current_lang).format(
                    dp_id=dp_id,
                    tg_name=tg_name,
                    amount=amount
                ),
                reply_markup=reply_markup
            )
            
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, notify_msg.message_id, 3))

            # Notify user
            keyboard = [
                [
                    InlineKeyboardButton(
                        get_translation('contact_support', current_lang),
                        url='https://t.me/m345_support'
                    ),
                    InlineKeyboardButton(
                        get_translation('ok_button', current_lang),
                        callback_data='delete_user_notify'
                    )
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                user_notify = await context.bot.send_message(
                    chat_id=user_id,
                    text=get_translation('deposit_approved_user', current_lang).format(
                        method=method,
                        tx_hash=tx_hash,
                        amount=amount
                    ),
                    reply_markup=reply_markup
                )
                asyncio.create_task(delete_after_delay(context, user_id, user_notify.message_id, 30))
            except Exception as e:
                logger.error(f"Could not notify user {user_id}: {e}")

        elif action == 'reject':
            # Execute the rejection with retries
            for attempt in range(MAX_RETRIES):
                try:
                    with sqlite3.connect('lottery.db') as conn:
                        cursor = conn.cursor()
                        conn.execute('BEGIN IMMEDIATE TRANSACTION')
                        cursor.execute('''
                            DELETE FROM deposits 
                            WHERE dp_id = ?
                        ''', (dp_id,))
                        conn.commit()
                        break
                except OperationalError as e:
                    if "database is locked" in str(e) and attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(RETRY_DELAY * (attempt + 1))
                        continue
                    raise

        # Refresh the deposit list
        await confirmdeposit_message(update, context)

    except Exception as e:
        logger.error(f"Error in handle_deposit_confirmation: {e}")
        error_msg = await update.message.reply_text(
            get_translation('error_occurred', current_lang).format(error=str(e)) + "\n\n" +
            get_translation('command_usage', current_lang)
        )
        asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))

async def delete_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    """Delete a message after a specified delay"""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Could not delete message: {e}")

async def handle_user_notification_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the OK button in user notifications"""
    query = update.callback_query
    await query.answer()
    try:
        if query.data == 'delete_user_notify':
            await query.delete_message()
    except Exception as e:
        logger.error(f"Error handling user notification button: {e}")
        
async def handle_notification_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Extract message ID from callback data
    if query.data.startswith('delete_notify_'):
        try:
            msg_id = int(query.data.split('_')[-1])
            # Delete the notification message
            await query.delete_message()
            
            # Also delete the original admin command if it exists
            try:
                await context.bot.delete_message(
                    chat_id=query.message.chat_id,
                    message_id=msg_id
                )
            except Exception as e:
                logger.warning(f"Could not delete original message: {e}")
                
        except Exception as e:
            logger.error(f"Error handling notification button: {e}")

async def confirmwithdrawals_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        
        current_lang = context.user_data.get('language', 'en')
        offset = context.user_data.get('withdrawal_offset', 0)
        
        # Handle pagination actions
        if query.data == 'withdrawal_next':
            offset += 10
        elif query.data == 'withdrawal_prev':
            offset = max(0, offset - 10)
        
        context.user_data['withdrawal_offset'] = offset
        context.user_data['withdrawal_list_active'] = True
        
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM withdrawals')
            total_withdrawals = cursor.fetchone()[0]
            
            cursor.execute('''
                SELECT wd_id, user_id, tg_name, wd_amt, wd_type, address 
                FROM withdrawals 
                ORDER BY wd_id ASC 
                LIMIT 10 OFFSET ?
            ''', (offset,))
            withdrawals = cursor.fetchall()

        if not withdrawals:
            message = get_translation('no_pending_withdrawals', current_lang)
        else:
            range_start = offset + 1
            range_end = min(offset + 10, total_withdrawals)
            message = get_translation('withdrawals_header', current_lang).format(
                range_start=range_start,
                range_end=range_end,
                total=total_withdrawals
            ) + "\n\n"
            
            for withdrawal in withdrawals:
                wd_id, user_id, tg_name, wd_amt, wd_type, address = withdrawal
                message += (
                    f"{get_translation('id_label', current_lang)}: {wd_id}\n"
                    f"{get_translation('user_label', current_lang)}: {tg_name} ({get_translation('id_label', current_lang)}: {user_id})\n"
                    f"{get_translation('amount_label', current_lang)}: ${wd_amt:.2f}\n"
                    f"{get_translation('method_label', current_lang)}: {wd_type}\n"
                    f"{get_translation('address_label', current_lang)}: {address}\n\n"
                    f"{get_translation('approve_command', current_lang)}: approve {wd_id}\n"
                    f"{get_translation('reject_command', current_lang)}: reject {wd_id}\n"
                    f"{get_translation('separator', current_lang)}\n"
                )

        keyboard = []
        if total_withdrawals > 10:
            keyboard.append([
                InlineKeyboardButton(
                    get_translation('prev_button', current_lang), 
                    callback_data='withdrawal_prev'
                ) if offset > 0 else InlineKeyboardButton(" ", callback_data='noop'),
                InlineKeyboardButton(
                    get_translation('next_button', current_lang), 
                    callback_data='withdrawal_next'
                ) if offset + 10 < total_withdrawals else InlineKeyboardButton(" ", callback_data='noop')
            ])
        keyboard.append([
            InlineKeyboardButton(
                get_translation('back_button', current_lang), 
                callback_data='back_to_adminstart'
            )
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(message, reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"Error in confirmwithdrawals_message: {e}")
        error_msg = get_translation('error_loading_withdrawals', context.user_data.get('language', 'en'))
        await query.edit_message_text(error_msg)

async def handle_withdrawal_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Withdrawal confirmation handler triggered with: {update.message.text}")
    logger.info(f"User data: {context.user_data}")
    try:
        current_lang = context.user_data.get('language', 'en')
        
        # Check if message is from admin
        if update.message.from_user.id != ADMIN_ID:
            await update.message.delete()
            return

        # Check if withdrawals list is active
        if not context.user_data.get('withdrawal_list_active', False):
            error_msg = await update.message.reply_text(
                get_translation('view_withdrawals_first', current_lang)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return

        # Delete admin input after processing
        asyncio.create_task(delete_after_delay(context, update.message.chat_id, update.message.message_id, 3))

        command = update.message.text.strip().lower()
        parts = command.split()
        
        # Validate command format
        if len(parts) != 2:
            error_msg = await update.message.reply_text(
                get_translation('invalid_withdrawal_format', current_lang)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return
            
        action = parts[0]
        wd_id = parts[1]
        
        # Validate action
        if action not in ['approve', 'reject']:
            error_msg = await update.message.reply_text(
                get_translation('invalid_withdrawal_action', current_lang)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return
            
        # Validate withdrawal ID
        try:
            wd_id = int(wd_id)
        except ValueError:
            error_msg = await update.message.reply_text(
                get_translation('invalid_withdrawal_id', current_lang)
            )
            asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
            return

        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            conn.execute('BEGIN TRANSACTION')

            # Get withdrawal details
            cursor.execute('''
                SELECT user_id, tg_name, wd_amt, wd_type, address 
                FROM withdrawals 
                WHERE wd_id = ?
            ''', (wd_id,))
            withdrawal = cursor.fetchone()

            if not withdrawal:
                error_msg = await update.message.reply_text(
                    get_translation('withdrawal_not_found', current_lang).format(wd_id=wd_id)
                )
                asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))
                return

            user_id, tg_name, wd_amt, wd_type, address = withdrawal

            if action == 'approve':
                # Check user balance
                cursor.execute('SELECT user_balance FROM users WHERE user_id = ?', (user_id,))
                balance = cursor.fetchone()[0]
                
                if balance >= wd_amt:
                    # Sufficient balance - process approval
                    cursor.execute('''
                        UPDATE users 
                        SET user_balance = user_balance - ? 
                        WHERE user_id = ?
                    ''', (wd_amt, user_id))
                    
                    # Delete the withdrawal record
                    cursor.execute('DELETE FROM withdrawals WHERE wd_id = ?', (wd_id,))
                    conn.commit()

                    # Notify admin
                    admin_msg = await update.message.reply_text(
                        get_translation('withdrawal_approved_admin', current_lang).format(
                            wd_id=wd_id,
                            tg_name=tg_name,
                            wd_amt=wd_amt,
                            wd_type=wd_type
                        )
                    )
                    asyncio.create_task(delete_after_delay(context, update.message.chat_id, admin_msg.message_id, 3))

                    # Notify user
                    try:
                        user_msg = await context.bot.send_message(
                            chat_id=user_id,
                            text=get_translation('withdrawal_approved_user', current_lang).format(
                                wd_amt=wd_amt,
                                wd_type=wd_type
                            )
                        )
                        asyncio.create_task(delete_after_delay(context, user_id, user_msg.message_id, 10))
                    except Exception as e:
                        logger.error(f"Could not notify user {user_id}: {e}")

                else:
                    # Insufficient balance - reject automatically
                    cursor.execute('DELETE FROM withdrawals WHERE wd_id = ?', (wd_id,))
                    conn.commit()

                    # Notify admin
                    admin_msg = await update.message.reply_text(
                        get_translation('auto_rejected_admin', current_lang).format(
                            wd_id=wd_id,
                            tg_name=tg_name,
                            wd_amt=wd_amt,
                            balance=balance
                        )
                    )
                    asyncio.create_task(delete_after_delay(context, update.message.chat_id, admin_msg.message_id, 3))

                    # Notify user
                    try:
                        user_msg = await context.bot.send_message(
                            chat_id=user_id,
                            text=get_translation('auto_rejected_user', current_lang).format(
                                wd_amt=wd_amt
                            )
                        )
                        asyncio.create_task(delete_after_delay(context, user_id, user_msg.message_id, 10))
                    except Exception as e:
                        logger.error(f"Could not notify user {user_id}: {e}")

            elif action == 'reject':
                # Delete the withdrawal record
                cursor.execute('DELETE FROM withdrawals WHERE wd_id = ?', (wd_id,))
                conn.commit()

                # Notify admin only
                admin_msg = await update.message.reply_text(
                    get_translation('withdrawal_rejected_admin', current_lang).format(
                        wd_id=wd_id,
                        tg_name=tg_name
                    )
                )
                asyncio.create_task(delete_after_delay(context, update.message.chat_id, admin_msg.message_id, 3))

        # Refresh the withdrawals list
        await confirmwithdrawals_message(update, context)

    except Exception as e:
        logger.error(f"Error in handle_withdrawal_confirmation: {e}")
        error_msg = await update.message.reply_text(
            get_translation('withdrawal_error', current_lang).format(error=str(e)) + "\n\n" +
            get_translation('withdrawal_usage', current_lang)
        )
        asyncio.create_task(delete_after_delay(context, update.message.chat_id, error_msg.message_id, 5))

async def delete_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int):
    """Delete a message after a specified delay"""
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Could not delete message: {e}")

async def back_to_adminstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    user = update.callback_query.from_user
    
    if user.id == ADMIN_ID:
        keyboard = [
            [InlineKeyboardButton(get_translation('deposits', current_lang), callback_data='confirmdeposit')],
            [InlineKeyboardButton(get_translation('withdrawals', current_lang), callback_data='confirmwithdrawals')],
            [InlineKeyboardButton(get_translation('enter_game', current_lang), callback_data='entergame')],
            [InlineKeyboardButton(get_translation('terms_conditions', current_lang), url='https://t.me/m345_support')],
            [InlineKeyboardButton(get_translation('customer_service', current_lang), url='https://t.me/m345_support')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        start_message = get_translation('welcome_admin', current_lang)
    else:
        keyboard = [
            [InlineKeyboardButton(get_translation('enter_game', current_lang), callback_data='entergame')],
            [InlineKeyboardButton(get_translation('change_language', current_lang), callback_data='languages')],
            [InlineKeyboardButton(get_translation('refer', current_lang), url='')],
            [InlineKeyboardButton(get_translation('terms_conditions', current_lang), url='https://t.me/m345_support')],
            [InlineKeyboardButton(get_translation('customer_service', current_lang), url='https://t.me/m345_support')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        start_message = get_translation('welcome_user', current_lang)
    
    await update.callback_query.edit_message_text(start_message, reply_markup=reply_markup)

async def entergame_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    keyboard = [
        [InlineKeyboardButton(get_translation('instructions', current_lang), callback_data='instructions')],
        [InlineKeyboardButton(get_translation('stake', current_lang), callback_data='stake')],
        [InlineKeyboardButton(get_translation('account', current_lang), callback_data='account')],
        [InlineKeyboardButton(get_translation('customer_service', current_lang), url='https://t.me/m345_support')],
        [InlineKeyboardButton(get_translation('exit', current_lang), callback_data='exit')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        text=get_translation('game_welcome_message', current_lang),
        reply_markup=reply_markup
    )

async def exit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    await update.callback_query.delete_message()
    user = update.callback_query.from_user
    user_id = user.id
    user_name = user.username or user.first_name
    tg_name = user.first_name

    # Add user and get referral code
    referral_code = add_user(user_id, user_name, tg_name)
    # Check if user is admin
    if ADMIN_ID and user_id == ADMIN_ID:
        # Admin keyboard
        keyboard = [
            [InlineKeyboardButton("Deposits", callback_data='confirmdeposit')],
            [InlineKeyboardButton("Withdrawals", callback_data='confirmwithdrawals')],
            [InlineKeyboardButton("Enter Game", callback_data='entergame')],
            [InlineKeyboardButton("Terms & Conditions", url='https://t.me/m345_support')],
            [InlineKeyboardButton("Customer Service", url='https://t.me/m345_support')]
        ]
        start_message = get_translation('welcome_admin', current_lang)
    else:
        # Regular user keyboard
        # Check if coming from referral link
        if context.args and len(context.args) == 1:
            referral_code_from_link = context.args[0]
            await handle_referral(context, user_id, referral_code_from_link)

        # Generate referral link
        bot_username = (await context.bot.get_me()).username
        referral_link = f"https://t.me/share/url?url=https://t.me/{bot_username}?start={referral_code}&text=Predict%205%20numbers%20and%20stand%20a%20chance%20of%20winning%20if%20you%20match%203,%204%20or%205%20numbers%20with%20the%20daily%20draw%20at%208PM"

        add_user(user_id, user_name, tg_name)

        keyboard = [
            [InlineKeyboardButton(get_translation('enter_game', current_lang), callback_data='entergame')],
            [InlineKeyboardButton(get_translation('change_language', current_lang), callback_data='languages')],
            [InlineKeyboardButton(get_translation('refer', current_lang), url=referral_link)],
            [InlineKeyboardButton(get_translation('terms_conditions', current_lang), url='https://t.me/m345_support')],
            [InlineKeyboardButton(get_translation('customer_service', current_lang), url='https://t.me/m345_support')]
        ]
        start_message = get_translation('welcome_user', current_lang)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat_id=update.callback_query.message.chat_id, text=start_message, reply_markup=reply_markup)

async def instru_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    keyboard = [[InlineKeyboardButton(get_translation('back_button', current_lang), callback_data='back_to_entergame')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        text=get_translation('how_to_bet_header', current_lang) + "\n\n" +
             get_translation('bet_description', current_lang) + "\n" +
             get_translation('how_to_play_header', current_lang) + "\n" +
             get_translation('step_1', current_lang) + "\n" +
             get_translation('step_2', current_lang) + "\n" +
             get_translation('step_3', current_lang) + "\n" +
             get_translation('step_4', current_lang) + "\n\n" +
             get_translation('prize_structure_header', current_lang) + "\n" +
             get_translation('prize_3_matches', current_lang) + "\n" +
             get_translation('prize_4_matches', current_lang) + "\n" +
             get_translation('prize_5_matches', current_lang),
        reply_markup=reply_markup
    )

async def stake_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        user = update.callback_query.from_user
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ticket_code, user_input, stake_amount, created_at FROM tickets WHERE user_id = ? ORDER BY created_at ASC LIMIT 10', 
                          (user.id,))
            tickets = cursor.fetchall()

        message = get_translation('your_games_header', current_lang) + "\n\n"
        if tickets:
            for ticket in tickets:
                ticket_code, user_input, stake_amount, created_at = ticket
                message += (
                    f"{get_translation('ticket_label', current_lang)}: {ticket_code}\n"
                    f"{get_translation('numbers_label', current_lang)}: {user_input}\n"
                    f"{get_translation('amount_label', current_lang)}: ${stake_amount:.2f}\n"
                    f"{get_translation('date_label', current_lang)}: {created_at}\n\n"
                )
        else:
            message += get_translation('no_active_tickets', current_lang) + "\n\n"
        
        message += get_translation('prediction_format', current_lang)

        keyboard = [
            [InlineKeyboardButton(get_translation('tickets_button', current_lang), callback_data='tickets')],
            [InlineKeyboardButton(get_translation('back_button', current_lang), callback_data='back_to_entergame')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text=message, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in stake_message: {e}")
        error_msg = get_translation('fetch_tickets_error', context.user_data.get('language', 'en'))
        await update.callback_query.edit_message_text(error_msg)

async def delete_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int = 5):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Could not delete message: {e}")

async def handle_stake_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        user = update.message.from_user
        user_input = update.message.text.strip()
        chat_id = update.message.chat_id
        
        # Validate input format
        if not re.match(r'^(\d{1,2},){4}\d{1,2}:\d+$', user_input):
            try:
                await update.message.delete()
            except:
                logger.warning(get_translation('delete_message_failed', current_lang))
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=get_translation('invalid_format_message', current_lang)
            )
            context.job_queue.run_once(
                lambda ctx: delete_after_delay(ctx, chat_id, msg.message_id),
                when=5
            )
            return
        
        # Parse numbers and amount
        numbers_part, amount_part = user_input.split(':')
        try:
            numbers = [int(n) for n in numbers_part.split(',')]
            amount = float(amount_part)
        except ValueError:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=get_translation('invalid_amount_message', current_lang)
            )
            context.job_queue.run_once(
                lambda ctx: delete_after_delay(ctx, chat_id, msg.message_id),
                when=5
            )
            return
        
        # Validate numbers range and uniqueness
        if len(numbers) != 5 or any(n < 1 or n > 50 for n in numbers) or len(set(numbers)) != 5:
            try:
                await update.message.delete()
            except:
                logger.warning(get_translation('delete_message_failed', current_lang))
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=get_translation('invalid_numbers_message', current_lang)
            )
            context.job_queue.run_once(
                lambda ctx: delete_after_delay(ctx, chat_id, msg.message_id),
                when=5
            )
            return
            
        # Validate amount range
        if amount < 1 or amount > 200:
            try:
                await update.message.delete()
            except:
                logger.warning(get_translation('delete_message_failed', current_lang))
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=get_translation('invalid_amount_range', current_lang)
            )
            context.job_queue.run_once(
                lambda ctx: delete_after_delay(ctx, chat_id, msg.message_id),
                when=5
            )
            return
            
        # Check user balance
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_balance FROM users WHERE user_id = ?', (user.id,))
            result = cursor.fetchone()
            
            if not result:  # User doesn't exist in database
                add_user(user.id, user.username or user.first_name, user.first_name)
                balance = 0.0
            else:
                balance = result[0]
            
            if balance < amount:
                try:
                    await update.message.delete()
                except:
                    logger.warning(get_translation('delete_message_failed', current_lang))
                msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=get_translation('insufficient_balance', current_lang)
                )
                context.job_queue.run_once(
                    lambda ctx: delete_after_delay(ctx, chat_id, msg.message_id),
                    when=5
                )
                return
                
            # Generate ticket code
            ticket_code = hashlib.md5(f"{user.id}{datetime.now()}".encode()).hexdigest()[:8].upper()
            
            # Create ticket
            cursor.execute('''
                INSERT INTO tickets (user_id, tg_name, ticket_code, stake_amount, user_input)
                VALUES (?, ?, ?, ?, ?)
            ''', (user.id, user.first_name, ticket_code, amount, numbers_part))
            
            # Deduct from balance
            cursor.execute('UPDATE users SET user_balance = user_balance - ? WHERE user_id = ?', 
                         (amount, user.id))
            conn.commit()
            
            try:
                await update.message.delete()
            except:
                logger.warning(get_translation('delete_message_failed', current_lang))
            
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=get_translation('bet_success_message', current_lang).format(ticket_code=ticket_code)
            )
            context.job_queue.run_once(
                lambda ctx: delete_after_delay(ctx, chat_id, msg.message_id),
                when=5
            )
            
    except Exception as e:
        logger.error(f"Error in handle_stake_message: {e}")
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=get_translation('bet_error_message', current_lang)
        )
        context.job_queue.run_once(
            lambda ctx: delete_after_delay(ctx, chat_id, msg.message_id),
            when=5
        )

async def tickets_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        user = update.callback_query.from_user
        offset = context.user_data.get('tickets_offset', 0)
        
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT ticket_code, user_input, stake_amount, created_at FROM tickets WHERE user_id = ? ORDER BY created_at ASC LIMIT 10 OFFSET ?', 
                         (user.id, offset))
            tickets = cursor.fetchall()

        message = get_translation('your_tickets_header', current_lang) + "\n\n"
        if tickets:
            for ticket in tickets:
                ticket_code, user_input, stake_amount, created_at = ticket
                message += (
                    f"{get_translation('ticket_label', current_lang)}: {ticket_code}\n"
                    f"{get_translation('numbers_label', current_lang)}: {user_input}\n"
                    f"{get_translation('amount_label', current_lang)}: ${stake_amount:.2f}\n"
                    f"{get_translation('date_label', current_lang)}: {created_at}\n\n"
                )
        else:
            if offset == 0:
                message += get_translation('no_tickets_yet', current_lang)
            else:
                message += get_translation('no_more_tickets', current_lang)

        keyboard = []
        if offset > 0 or len(tickets) == 10:
            keyboard.append([
                InlineKeyboardButton(
                    get_translation('prev_button', current_lang), 
                    callback_data='tickets_prev'
                ) if offset > 0 else InlineKeyboardButton(" ", callback_data='none'),
                InlineKeyboardButton(
                    get_translation('next_button', current_lang), 
                    callback_data='tickets_next'
                ) if len(tickets) == 10 else InlineKeyboardButton(" ", callback_data='none')
            ])
        keyboard.append([
            InlineKeyboardButton(
                get_translation('back_button', current_lang), 
                callback_data='back_to_entergame'
            )
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.callback_query.edit_message_text(text=message, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in tickets_message: {e}")
        error_msg = get_translation('tickets_error', context.user_data.get('language', 'en'))
        await update.callback_query.edit_message_text(error_msg)

async def back_to_entergame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await entergame_message(update, context)

async def back_to_stake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await stake_message(update, context)

async def account_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    user_id = update.callback_query.from_user.id
    
    with sqlite3.connect('lottery.db') as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT tg_name, language, user_balance FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        
        if result is None:
            # User doesn't exist, create them
            user = update.callback_query.from_user
            tg_name = user.first_name
            add_user(user_id, user.username or tg_name, tg_name)
            # Get the newly created user's data
            cursor.execute('SELECT tg_name, language, user_balance FROM users WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
        
        # Unpack the result
        tg_name, language, balance = result if result else (user.first_name, current_lang, 0.0)
        
        # Update context with the correct language from DB
        context.user_data['language'] = language
        current_lang = language

    # Format the message with all variables
    message = get_translation('balance_message', current_lang).format(
        tg_name=tg_name,
        language=language.upper(),  # Showing language code in uppercase (EN, ES, etc.)
        balance=float(balance)
    )

    keyboard = [
        [InlineKeyboardButton(get_translation('deposit_button', current_lang), callback_data='deposit')],
        [InlineKeyboardButton(get_translation('withdraw_button', current_lang), callback_data='withdrawal')],
        [InlineKeyboardButton(get_translation('back_button', current_lang), callback_data='back_to_entergame')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(text=message, reply_markup=reply_markup)

async def languages_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        current_lang = context.user_data.get('language', 'en')
        
        keyboard = [
            [InlineKeyboardButton("English", callback_data='language_en')],
            [InlineKeyboardButton("Español", callback_data='language_es')],
            [InlineKeyboardButton("Русский", callback_data='language_ru')],
            [InlineKeyboardButton("Deutsch", callback_data='language_de')],
            [InlineKeyboardButton("Français", callback_data='language_fr')],
            [InlineKeyboardButton("العربية", callback_data='language_ar')],
            [InlineKeyboardButton(get_translation('back_button', current_lang), callback_data='exit')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            text=get_translation('select_language_prompt', current_lang),
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Error in languages_message: {e}")
        error_msg = get_translation('language_error', context.user_data.get('language', 'en'))
        await update.callback_query.edit_message_text(error_msg)

async def set_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()
        
        # Extract language code from callback_data (format: language_xx)
        language = query.data.split('_')[-1]
        context.user_data['language'] = language
        
        # Update database
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET language = ? WHERE user_id = ?', 
                         (language, query.from_user.id))
            conn.commit()
        
        # Show confirmation message in the new language
        await query.edit_message_text(
            text=get_translation('language_set_confirmation', language)
        )
        
        # Send the start message with the new language
        user = query.from_user
        user_id = user.id
        user_name = user.username or user.first_name
        tg_name = user.first_name

        # Check if user is admin
        if ADMIN_ID and user_id == ADMIN_ID:
            # Admin keyboard
            keyboard = [
                [InlineKeyboardButton(get_translation('deposits', language), callback_data='confirmdeposit')],
                [InlineKeyboardButton(get_translation('withdrawals', language), callback_data='confirmwithdrawals')],
                [InlineKeyboardButton(get_translation('enter_game', language), callback_data='entergame')],
                [InlineKeyboardButton(get_translation('terms_conditions', language), url='https://t.me/m345_support')],
                [InlineKeyboardButton(get_translation('customer_service', language), url='https://t.me/m345_support')],
                [InlineKeyboardButton(get_translation('change_language', language), callback_data='languages')]
            ]
            start_message = get_translation('welcome_admin', language)
        else:
            # Regular user keyboard
            keyboard = [
                [InlineKeyboardButton(get_translation('enter_game', language), callback_data='entergame')],
                [InlineKeyboardButton(get_translation('change_language', language), callback_data='languages')],
                [InlineKeyboardButton(get_translation('terms_conditions', language), url='https://t.me/m345_support')],
                [InlineKeyboardButton(get_translation('customer_service', language), url='https://t.me/m345_support')]
            ]
            start_message = get_translation('welcome_user', language)

        reply_markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=user_id,
            text=start_message,
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Error in set_language: {e}")
        await update.callback_query.edit_message_text(
            get_translation('language_change_error', context.user_data.get('language', 'en'))
        )

async def deposit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    context.user_data['deposit_active'] = True
    
    keyboard = [[InlineKeyboardButton(get_translation('back_button', current_lang), callback_data='back_to_account')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        text=get_translation('make_payment_header', current_lang),
        reply_markup=reply_markup
    )

async def handle_deposit_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        if not context.user_data.get('deposit_active', False):
            return

        user = update.message.from_user
        user_input = update.message.text.strip()
        chat_id = update.message.chat_id
        message_id = update.message.message_id

        valid_methods = ['ETH', 'BNB', 'USDT', 'BTC']
        input_parts = user_input.split()
        
        if len(input_parts) != 2 or input_parts[1].upper() not in valid_methods:
            await handle_invalid_deposit_input(update, context)
            return

        tx_hash, method = input_parts
        method = method.upper()

        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO deposits (user_id, user_name, tg_name, dp_meth, tx_hash)
                VALUES (?, ?, ?, ?, ?)
            ''', (user.id, user.username or user.first_name, user.first_name, method, tx_hash))
            conn.commit()

        await handle_successful_deposit(update, context)

        # After successful deposit recording:
        await handle_successful_deposit(update, context)

    except Exception as e:
        logger.error(f"Error in handle_deposit_message: {e}")
        await handle_deposit_error(update, context)

async def handle_invalid_deposit_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        try:
            await update.message.delete()
        except Exception as delete_error:
            logger.error(f"Could not delete user message: {delete_error}")
            return

        error_msg = await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=get_translation('invalid_deposit_format', current_lang) + "\n" +
                 get_translation('valid_methods', current_lang) + "\n\n" +
                 get_translation('tx_example', current_lang)
        )
        
        await asyncio.sleep(3)
        try:
            await error_msg.delete()
        except Exception as delete_error:
            logger.error(f"Could not delete error message: {delete_error}")

    except Exception as e:
        logger.error(f"Error in handle_invalid_deposit_input: {e}")

async def handle_successful_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        try:
            await update.message.delete()
        except Exception as delete_error:
            logger.error(f"Could not delete original message: {delete_error}")

        success_msg = await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=get_translation('deposit_success', current_lang)
        )
        
        await asyncio.sleep(3)
        try:
            await success_msg.delete()
        except Exception as delete_error:
            logger.error(f"Could not delete success message: {delete_error}")

    except Exception as e:
        logger.error(f"Error in handle_successful_deposit: {e}")

async def handle_deposit_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle general deposit errors"""
    try:
        # Try to send and delete error message
        error_msg = await context.bot.send_message(
            chat_id=update.message.chat_id,
            text="⚠️ An error occurred while processing your deposit. Please try again."
        )
        await asyncio.sleep(3)
        try:
            await error_msg.delete()
        except Exception as delete_error:
            logger.error(f"Could not delete error message: {delete_error}")
    except Exception as e:
        logger.error(f"Error in handle_deposit_error: {e}")

async def withdrawal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    context.user_data['withdrawal_active'] = True
    
    keyboard = [[InlineKeyboardButton(get_translation('back_button', current_lang), callback_data='back_to_account')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        text=get_translation('withdrawal_header', current_lang) + "\n\n" +
             get_translation('withdrawal_format_instructions', current_lang) + "\n\n" +
             get_translation('withdrawal_example', current_lang) + "\n" +
             get_translation('valid_methods', current_lang) + "\n" +
             get_translation('withdrawal_limits', current_lang),
        reply_markup=reply_markup
    )

async def handle_withdrawal_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        current_lang = context.user_data.get('language', 'en')
        if not context.user_data.get('withdrawal_active', False):
            return

        user = update.message.from_user
        user_input = update.message.text.strip()
        chat_id = update.message.chat_id

        if not re.match(r'^\d+(\.\d{1,2})?\s+(ETH|BNB|USDT|BTC)\s+[\w\-]+', user_input, re.IGNORECASE):
            await handle_invalid_withdrawal_input(update, context)
            return

        try:
            amount_str, method, address = user_input.split(maxsplit=2)
            amount = float(amount_str)
            method = method.upper()
        except ValueError:
            await handle_invalid_withdrawal_input(update, context)
            return

        if amount < 1 or amount > 500:
            await handle_invalid_withdrawal_amount(update, context)
            return

        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT user_balance FROM users WHERE user_id = ?', (user.id,))
            result = cursor.fetchone()
            
            if not result:
                await handle_user_not_found(update, context)
                return
                
            balance = result[0]
            
            if balance < amount:
                await handle_insufficient_balance(update, context)
                return
                
            cursor.execute('''
                INSERT INTO withdrawals (user_id, user_name, tg_name, wd_type, wd_amt, address)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user.id, user.username or user.first_name, user.first_name, method, amount, address))
            conn.commit()

        await handle_successful_withdrawal_request(update, context)

    except Exception as e:
        logger.error(f"Error in handle_withdrawal_message: {e}")
        await handle_withdrawal_error(update, context)

async def handle_invalid_withdrawal_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Could not delete message: {e}")

    error_msg = await context.bot.send_message(
        chat_id=update.message.chat_id,
        text=get_translation('invalid_withdrawal_format', current_lang) + "\n" +
             get_translation('withdrawal_format_instructions', current_lang) + "\n\n" +
             get_translation('withdrawal_example', current_lang) + "\n" +
             get_translation('valid_methods', current_lang)
    )
    
    await asyncio.sleep(5)
    try:
        await error_msg.delete()
    except Exception as e:
        logger.error(f"Could not delete error message: {e}")

async def handle_invalid_withdrawal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Could not delete message: {e}")

    error_msg = await context.bot.send_message(
        chat_id=update.message.chat_id,
        text=get_translation('invalid_withdrawal_amount', current_lang)
    )
    
    await asyncio.sleep(3)
    try:
        await error_msg.delete()
    except Exception as e:
        logger.error(f"Could not delete error message: {e}")

async def handle_insufficient_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Could not delete message: {e}")

    error_msg = await context.bot.send_message(
        chat_id=update.message.chat_id,
        text=get_translation('insufficient_balance_withdrawal', current_lang)
    )
    
    await asyncio.sleep(3)
    try:
        await error_msg.delete()
    except Exception as e:
        logger.error(f"Could not delete error message: {e}")

async def handle_successful_withdrawal_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    current_lang = context.user_data.get('language', 'en')
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Could not delete original message: {e}")

    success_msg = await context.bot.send_message(
        chat_id=update.message.chat_id,
        text=get_translation('withdrawal_request_success', current_lang)
    )
    
    await asyncio.sleep(3)
    try:
        await success_msg.delete()
    except Exception as e:
        logger.error(f"Could not delete success message: {e}")

async def handle_withdrawal_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle general withdrawal errors"""
    current_lang = context.user_data.get('language', 'en')
    try:
        error_msg = await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=get_translation('withdrawal_error_general', current_lang)
        )
        await asyncio.sleep(3)
        try:
            await error_msg.delete()
        except Exception as e:
            logger.error(get_translation('delete_error_failed', current_lang))
    except Exception as e:
        logger.error(f"Error in handle_withdrawal_error: {e}")

async def handle_user_not_found(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle case where user isn't found in database"""
    current_lang = context.user_data.get('language', 'en')
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(get_translation('delete_message_failed', current_lang))

    error_msg = await context.bot.send_message(
        chat_id=update.message.chat_id,
        text=get_translation('user_not_found_error', current_lang)
    )
    
    await asyncio.sleep(3)
    try:
        await error_msg.delete()
    except Exception as e:
        logger.error(get_translation('delete_error_failed', current_lang))

async def back_to_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Clear deposit_active flag
    context.user_data['deposit_active'] = False
    await account_message(update, context)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Invalid option selected.", show_alert=True)

async def daily_draw(context: ContextTypes.DEFAULT_TYPE):
    try:
        # Generate 5 unique random numbers between 1 and 50
        draw_numbers = random.sample(range(1, 51), 5)
        draw_str = ','.join(map(str, sorted(draw_numbers)))
        
        # Save the draw to database
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT INTO draws (draw) VALUES (?)', (draw_str,))
            conn.commit()
            
            # Get all tickets from today
            cursor.execute('''
                SELECT ticket_id, user_id, tg_name, ticket_code, stake_amount, user_input, created_at 
                FROM tickets 
                WHERE date(created_at) = date('now')
            ''')
            tickets = cursor.fetchall()
            
            for ticket in tickets:
                ticket_id, user_id, tg_name, ticket_code, stake_amount, user_input, created_at = ticket
                user_numbers = list(map(int, user_input.split(',')))
                
                # Count matches
                matches = len(set(user_numbers) & set(draw_numbers))
                
                if matches >= 3:
                    # Calculate winnings and commissions
                    win_amount = stake_amount * PRIZES[matches]
                    commission = stake_amount * (PRIZES[matches] / 10)  # 10% commission
                    
                    # Record winner
                    cursor.execute('''
                        INSERT INTO winners (ticket_id, user_id, tg_name, ticket_code, stake_amount, win_amount, user_input, stake_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (ticket_id, user_id, tg_name, ticket_code, stake_amount, win_amount, user_input, created_at))
                    
                    # Record commission
                    cursor.execute('INSERT INTO commissions (commissions) VALUES (?)', (commission,))
                    
                    # Update user balance
                    cursor.execute('UPDATE users SET user_balance = user_balance + ? WHERE user_id = ?', 
                                 (win_amount, user_id))
                    
                    conn.commit()
                    
    except Exception as e:
        logger.error(f"Error in daily_draw: {e}")

async def announce_winners(context: ContextTypes.DEFAULT_TYPE):
    try:
        with sqlite3.connect('lottery.db') as conn:
            cursor = conn.cursor()
            
            # Get today's draw
            cursor.execute('SELECT draw FROM draws ORDER BY draw_id DESC LIMIT 1')
            draw_result = cursor.fetchone()
            draw = draw_result[0] if draw_result else get_translation('no_draw_yet', 'en')
            
            # Get today's winners
            cursor.execute('''
                SELECT tg_name, user_input, stake_amount, win_amount 
                FROM winners 
                WHERE date(created_at) = date('now')
                ORDER BY win_amount DESC
            ''')
            winners = cursor.fetchall()
            
            # Get all users with their preferred language
            cursor.execute('SELECT user_id, tg_name, language FROM users')
            users = cursor.fetchall()
            
            # Build messages for each language to optimize translations
            messages = {}
            for user_id, tg_name, language in users:
                current_lang = language if language else 'en'
                
                if current_lang not in messages:
                    # Build localized message template
                    message = get_translation('draw_announcement', current_lang).format(draw=draw) + "\n\n"
                    message += get_translation('winners_list_header', current_lang) + "\n\n"
                    
                    if winners:
                        for winner in winners:
                            winner_tg_name, user_input, stake_amount, win_amount = winner
                            message += (
                                f"{get_translation('user_label', current_lang)}: {winner_tg_name}\n"
                                f"{get_translation('stake_label', current_lang)}: {user_input}\n"
                                f"{get_translation('stake_amount_label', current_lang)}: ${stake_amount:.2f}\n"
                                f"{get_translation('won_amount_label', current_lang)}: ${win_amount:.2f}\n\n"
                            )
                    else:
                        message += get_translation('no_winners_today', current_lang) + "\n\n"
                    
                    messages[current_lang] = message
            
            # Send messages to all users
            for user_id, tg_name, language in users:
                current_lang = language if language else 'en'
                message = messages[current_lang]
                
                try:
                    await context.bot.send_message(chat_id=user_id, text=message)
                except Exception as e:
                    logger.error(f"Error sending to user {user_id} ({tg_name}): {e}")
                    
    except Exception as e:
        logger.error(f"Error in announce_winners: {e}")

# Add this command handler near the other command handlers in your code
async def announce_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to manually trigger winner announcements"""
    user_id = update.message.from_user.id
    
    # Check if user is admin
    if user_id != ADMIN_ID:
        await update.message.reply_text("This command is only available to administrators.")
        return
    
    try:
        await update.message.reply_text("Starting manual winner announcement...")
        await announce_winners(context)
        await update.message.reply_text("Winner announcement completed!")
    except Exception as e:
        logger.error(f"Error in announce_command: {e}")
        await update.message.reply_text("Failed to send announcements. Check logs for details.")

# Compile the regex pattern with flags
deposit_regex = re.compile(r'^[a-zA-Z0-9]+\s+(eth|bnb|btc|usdt)$', re.IGNORECASE)
withdrawal_regex = re.compile(r'^\d+\n', re.IGNORECASE)
prediction_regex = re.compile(r'^\d+(,\d+){4}:\d+')

def main():
    initialize_db()

    bot_token = os.getenv('BOT_TOKEN')
    if not bot_token:
        raise ValueError("No BOT_TOKEN environment variable set")

    application = Application.builder().token(bot_token).build()

    # Compile regex patterns with flags
    stake_pattern = re.compile(r'^(\d{1,2},){4}\d{1,2}:\d+$')
    deposit_pattern = re.compile(r'^[a-zA-Z0-9]+\s+(eth|bnb|btc|usdt)$', re.IGNORECASE)
    withdrawal_pattern = re.compile(r'^\d+(\.\d+)?\s+(eth|bnb|btc|usdt)\s+.+$', re.IGNORECASE)

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", adminstart))
    application.add_handler(CommandHandler("approve", handle_deposit_confirmation))
    application.add_handler(CommandHandler("reject", handle_withdrawal_confirmation))
    application.add_handler(CommandHandler("announce", announce_command))

    # Message handlers
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(stake_pattern),
        handle_stake_message
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(deposit_pattern),
        handle_deposit_message
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(withdrawal_pattern),
        handle_withdrawal_message
    ))
    # For deposit approvals (format: approve ID amount)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & 
        filters.Regex(r'^approve\s+\d+\s+\d*\.?\d+$'),
        handle_deposit_confirmation
    ))

    # For deposit rejections (format: reject ID)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & 
        filters.Regex(r'^reject\s+\d+$'),
        handle_deposit_confirmation
    ))
    # For withdrawal approvals/rejections (format: approve ID or reject ID)
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & 
        filters.Regex(r'^(approve|reject)\s+\d+$'),
        handle_withdrawal_confirmation
    ))
    

    # Callback query handlers
    # Callback query handlers - REORDERED with more specific patterns first
    application.add_handler(CallbackQueryHandler(
        set_language,
        pattern='^language_'
    ))  # This must come before the generic button_callback
    
    application.add_handler(CallbackQueryHandler(
        handle_notification_button,
        pattern=r'^delete_notify_\d+$'
    ))
    
    application.add_handler(CallbackQueryHandler(
        confirmwithdrawals_message,
        pattern='^(withdrawal_next|withdrawal_prev|noop)$'
    ))
    
    application.add_handler(CallbackQueryHandler(
        lambda update, context: delete_after_delay(context, update.callback_query.message.chat_id, update.callback_query.message.message_id, 0),
        pattern='^delete_notify_'
    ))
    
    # Other specific handlers
    application.add_handler(CallbackQueryHandler(confirmdeposit_message, pattern='confirmdeposit'))
    application.add_handler(CallbackQueryHandler(confirmwithdrawals_message, pattern='confirmwithdrawals'))
    application.add_handler(CallbackQueryHandler(entergame_message, pattern='entergame'))
    application.add_handler(CallbackQueryHandler(instru_message, pattern='instructions'))
    application.add_handler(CallbackQueryHandler(stake_message, pattern='stake'))
    application.add_handler(CallbackQueryHandler(tickets_message, pattern='tickets'))
    application.add_handler(CallbackQueryHandler(account_message, pattern='account'))
    application.add_handler(CallbackQueryHandler(languages_message, pattern='^languages$'))
    application.add_handler(CallbackQueryHandler(back_to_adminstart, pattern='back_to_adminstart'))
    application.add_handler(CallbackQueryHandler(back_to_stake, pattern='back_to_stake'))
    application.add_handler(CallbackQueryHandler(deposit_message, pattern='deposit'))
    application.add_handler(CallbackQueryHandler(withdrawal_message, pattern='withdrawal'))
    application.add_handler(CallbackQueryHandler(back_to_entergame, pattern='back_to_entergame'))
    application.add_handler(CallbackQueryHandler(back_to_account, pattern='back_to_account'))
    application.add_handler(CallbackQueryHandler(exit_message, pattern='exit'))
    
    # This should be the LAST handler as a fallback
    application.add_handler(CallbackQueryHandler(button_callback))
    # Schedule daily tasks
    job_queue = application.job_queue
    
    # Create time objects with UTC timezone
    utc = pytz.UTC
    draw_time = time(20, 0, 0).replace(tzinfo=utc)  # 8:00 PM UTC
    announce_time = time(20, 2, 0).replace(tzinfo=utc)  # 8:02 PM UTC
    
    # Schedule jobs
    job_queue.run_daily(daily_draw, time=draw_time, days=tuple(range(7)))
    job_queue.run_daily(announce_winners, time=announce_time, days=tuple(range(7)))

    application.run_polling()

if __name__ == '__main__':
    main()
