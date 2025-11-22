# sms/tasks.py
import os
import time
import pandas as pd
from celery import shared_task
from django.conf import settings
from .utils import send_via_cloudwhatsapp
from .models import Campaign, MessageLog
from django.contrib.auth.models import User

import logging
logger = logging.getLogger(__name__)

@shared_task(bind=True)
def send_bulk_whatsapp(self, campaign_id,user_id, excel_path, template, delay_seconds=1, img_url=None, pdf_url=None):
    """
    Send bulk WhatsApp using CloudWhatsApp API that expects media as public URLs.
    """
    campaign = None
    temp_files_to_clean = [excel_path]  # Only Excel is a temp file now
    logger.info("send_bulk_whatsapp starting...")
    try:
        campaign = Campaign.objects.get(id=campaign_id)
        user = User.objects.get(id=user_id)
        df = pd.read_excel(excel_path)
        total = len(df)
        campaign.total_numbers = total
        campaign.save()

        # api_keys = [k.strip() for k in getattr(settings, 'SMS_API_KEYS', []) if k.strip()]
        # if not api_keys:
        #     raise ValueError("No API keys configured")

        # current_key_index = 0
        api_key = user.userprofile.api_key  
        if not api_key:
            raise ValueError(f"User {user.username} does not have an API key assigned.")
        sent, failed = 0, 0

        for idx, row in df.iterrows():
            raw_phone = str(row.get('phone', '')).strip()
            if not raw_phone or not raw_phone.isdigit():
                failed += 1
                continue

            phone = raw_phone
            message = template
            for col in df.columns:
                if col == 'phone':
                    continue
                placeholder = f"{{{{{col}}}}}"
                if placeholder in message:
                    value = str(row.get(col, '')).strip()
                    message = message.replace(placeholder, value)
            message = message.strip()

            # Skip if nothing to send
            if not message and not img_url and not pdf_url:
                logger.warning(f"Nothing to send for {phone}")
                failed += 1
                continue

            # Use the URLs passed from view (no local paths!)
            api_key = api_keys[current_key_index]
            success, error = send_via_cloudwhatsapp(
                phone=phone,
                message=message,
                api_key=api_key,
                img_url=img_url,
                pdf_url=pdf_url
            )
            logger.info(f"Sending WhatsApp to {phone} with pdf_url: {pdf_url}")

            # Rotate key on auth errors
            if not success and ('invalid api key' in str(error).lower() or 'blocked' in str(error).lower()):
                current_key_index = (current_key_index + 1) % len(api_keys)
                api_key = api_keys[current_key_index]
                success, error = send_via_cloudwhatsapp(
                    phone=phone,
                    message=message,
                    api_key=api_key,
                    img_url=img_url,
                    pdf_url=pdf_url
                )

            # Log result
            MessageLog.objects.create(
                campaign=campaign,
                user=user,
                phone_number=phone,
                message_text=message,
                status='sent' if success else 'failed',
                error_code=str(error) if not success else None,
                api_key_used=api_key
            )
            logger.info(f"adding Data to MessageLog to {phone} and api used :{api_key}")
            if success:
                sent += 1
            else:
                failed += 1

            campaign.sent_count = sent
            campaign.failed_count = failed
            campaign.save(update_fields=['sent_count', 'failed_count'])

            if delay_seconds > 0:
                time.sleep(delay_seconds)

        logger.info(f"Campaign {campaign_id} finished: {sent} sent, {failed} failed")

    except Exception as e:
        logger.exception(f"Task failed for campaign {campaign_id}: {e}")
        raise
    finally:
        # Clean up ONLY the Excel temp file (media files are in MEDIA_ROOT and not deleted here)
        try:
            if excel_path and os.path.exists(excel_path):
                os.unlink(excel_path)
                logger.info(f"Temporary Excel file deleted: {excel_path}")
        except Exception as ex:
            logger.warning(f"Failed to delete Excel temp file {excel_path}: {ex}")
