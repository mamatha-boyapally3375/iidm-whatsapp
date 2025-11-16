# sms/views.py
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.core.files.storage import default_storage
from django.contrib.auth import authenticate, login, logout
from .models import Campaign, MessageLog
from .tasks import send_bulk_whatsapp
from .utils import save_uploaded_file_to_media
from django.db.models import Sum
import re
import os
import tempfile
import pandas as pd
from django.conf import settings
import time
from django.contrib.auth.decorators import login_required
import json
from django.contrib.sites.models import Site
from urllib.parse import urljoin
from django.core.files.uploadedfile import UploadedFile
import logging
logger = logging.getLogger(__name__)

@login_required
def upload_view(request):
    if request.method == 'POST':
        campaign_name = request.POST.get('campaign_name', '').strip()
        message_template = request.POST.get('message_template', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        excel_file = request.FILES.get('excel_file')
        img1 = request.FILES.get('img1')
        pdf = request.FILES.get('pdf')
        delay_input = request.POST.get('delay', '1')
        logger.info(f"getting info for the upload view...campaign name: {campaign_name},phone:{phone_number},exfile:{excel_file},img1:{img1}and pdf:{pdf}")

        # ====== 1. Validate core fields ======
        if not campaign_name:
            messages.error(request, "Campaign name is required.")
            return render(request, 'upload.html')
        if not message_template:
            messages.error(request, "Message template is required.")
            return render(request, 'upload.html')

        # ====== 2. Validate phone vs Excel mutual exclusion ======
        if phone_number and excel_file:
            messages.error(request, "Please provide either a phone number or an Excel file, not both.")
            return render(request, 'upload.html')
        if not phone_number and not excel_file:
            messages.error(request, "Either a phone number or an Excel file is required.")
            return render(request, 'upload.html')

        # ====== 3. Validate phone number (if provided) ======
        if phone_number:
            if not phone_number.isdigit():
                messages.error(request, "Phone number must contain digits only (no spaces, dashes, or symbols).")
                return render(request, 'upload.html')
            if len(phone_number) != 10:
                messages.error(request, "Phone number must be exactly 10 digits long.")
                return render(request, 'upload.html')
            # Note: Only one number allowed — input is single field, so this is enforced by UI/backend

        # ====== 4. Validate image and PDF mutual exclusion ======
        if img1 and pdf:
            messages.error(request, "You cannot upload both an image and a PDF. Please choose one.")
            return render(request, 'upload.html')

        # ====== 5. Validate file types and sizes (defense in depth) ======
        if img1:
            if not isinstance(img1, UploadedFile):
                messages.error(request, "Invalid image file.")
                return render(request, 'upload.html')
            valid_image_types = ['image/jpeg', 'image/png', 'image/jpg', 'image/gif']
            if img1.content_type not in valid_image_types:
                messages.error(request, "Please upload a valid image (JPEG, PNG, JPG, or GIF).")
                return render(request, 'upload.html')
            if img1.size > 1 * 1024 * 1024:  # 1 MB
                messages.error(request, "Image file must be under 1 MB.")
                return render(request, 'upload.html')

        if pdf:
            if not isinstance(pdf, UploadedFile):
                messages.error(request, "Invalid PDF file.")
                return render(request, 'upload.html')
            if pdf.content_type != 'application/pdf':
                messages.error(request, "Please upload a valid PDF file.")
                return render(request, 'upload.html')
            if pdf.size > 1 * 1024 * 1024:  # 1 MB
                messages.error(request, "PDF file must be under 1 MB.")
                return render(request, 'upload.html')

        if excel_file:
            if not isinstance(excel_file, UploadedFile):
                messages.error(request, "Invalid Excel file.")
                return render(request, 'upload.html')
            valid_excel_ext = ('.xlsx', '.xls')
            if not excel_file.name.lower().endswith(valid_excel_ext):
                messages.error(request, "Please upload a valid Excel file (.xlsx or .xls).")
                return render(request, 'upload.html')
            if excel_file.size > 10 * 1024 * 1024:  # 10 MB
                messages.error(request, "Excel file must be under 10 MB.")
                return render(request, 'upload.html')

        # ====== 6. Validate delay ======
        try:
            delay_seconds = int(delay_input)
            delay_seconds = max(0, min(delay_seconds, 60))
        except (ValueError, TypeError):
            delay_seconds = 1

        # ====== 7. Process media URLs (if any) ======
        img_url = None
        pdf_url = None
        try:
            if img1 or pdf:
                current_site = Site.objects.get_current()
                domain = current_site.domain.strip()
                base_url = f"https://{domain}"
                logger.info(f"BASE URL USED: {base_url}")

                if img1:
                    relative_path = save_uploaded_file_to_media(img1, "whatsapp/images")
                    img_url = f"{base_url}{relative_path}"
                    logger.info(f"IMG URL in view: {img_url}")


                if pdf:
                    relative_path = save_uploaded_file_to_media(pdf, "whatsapp/pdfs")
                    pdf_url = f"{base_url}{relative_path}"
                    logger.info(f"PDF URL in view: {pdf_url}")

        except Exception as e:
            messages.error(request, f"Failed to process media files: {str(e)}")
            logger.error(f"Failed to process media files: {str(e)}")

            return render(request, 'upload.html')

        # ====== 8. Prepare Excel file ======
        try:
            if phone_number:
                df = pd.DataFrame([{'phone': phone_number}])
                with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
                    df.to_excel(tmp, index=False)
                    excel_path = tmp.name
                total = 1
            else:
                # Validate Excel content (at least one phone number)
                df = pd.read_excel(excel_file)
                if df.empty:
                    messages.error(request, "Uploaded Excel file is empty.")
                    return render(request, 'upload.html')
                if 'phone' not in df.columns:
                    messages.error(request, "Excel file must contain a 'phone' column.")
                    return render(request, 'upload.html')
                # Optional: validate phone numbers in Excel
                phone_col = df['phone'].astype(str).str.replace(r'\D', '', regex=True)
                if phone_col.str.len().ne(10).any():
                    messages.error(request, "All phone numbers in Excel must be exactly 10 digits.")
                    return render(request, 'upload.html')
                if phone_col.str.contains(r'[^0-9]').any():
                    messages.error(request, "Phone numbers in Excel must contain only digits.")
                    return render(request, 'upload.html')

                with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as tmp:
                    df.to_excel(tmp, index=False)
                    excel_path = tmp.name
                total = len(df)
        except Exception as e:
            messages.error(request, f"Error processing Excel file: {str(e)}")
            return render(request, 'upload.html')

        # ====== 9. Save campaign ======
        campaign = Campaign.objects.create(
            user=request.user,
            name=campaign_name,
            template=message_template,
            total_numbers=total,
        )
        logging.info(f"campaign details:{campaign}")
        send_bulk_whatsapp.delay(
                campaign_id=campaign.id,
                user_id=request.user.id,
                excel_path=excel_path,
                template=message_template,
                delay_seconds=delay_seconds,
                img_url=img_url,
                pdf_url=pdf_url
            )
        logging. info(f"sending data to the bulf send_bulk_whatsapp..")
        messages.success(request, "Campaign started successfully!")
        return redirect('dashboard')

    return render(request, 'upload.html')

@login_required
def campaign_detail_view(request, campaign_id):
    # Ensure the campaign belongs to the current user
    campaign = get_object_or_404(Campaign, id=campaign_id, user=request.user)
    
    # Fetch logs: filter by BOTH campaign AND user (extra security)
    logs = MessageLog.objects.filter(
        campaign=campaign,
        user=request.user
    ).order_by('-timestamp')  # Most recent first

    # Prepare logs for JSON serialization
    logs_data = []
    for log in logs:
        logs_data.append({
            "phone": log.phone_number,
            "status": log.status,
            "error": log.error_code or "-",
            "key": log.api_key_used or "-",
        })

    # Campaign stats
    total = campaign.total_numbers or 0
    sent = campaign.sent_count or 0
    failed = campaign.failed_count or 0
    success_rate = round((sent / total) * 100, 2) if total > 0 else 0.0

    context = {
        'campaign': campaign,
        'logs_json': json.dumps(logs_data),  # ✅ Safe for JS
        'total': total,
        'sent': sent,
        'failed': failed,
        'success_rate': success_rate,
    }
    return render(request, 'campaign_detail.html', context)

@login_required
def dashboard_view(request):
    campaigns = Campaign.objects.filter(user=request.user).order_by('-created_at') # Replace with your model

    # Aggregate totals
    agg = campaigns.aggregate(
        total_sent=Sum('sent_count'),
        total_failed=Sum('failed_count')
    )
    total_sent = agg['total_sent'] or 0
    total_failed = agg['total_failed'] or 0
    total_processed = total_sent + total_failed

    # Calculate rates
    if total_processed > 0:
        success_rate = (total_sent / total_processed) * 100
        failure_rate = (total_failed / total_processed) * 100
    else:
        success_rate = 0.0
        failure_rate = 0.0

    context = {
        'campaigns': campaigns,
        'total_sent': total_sent,
        'success_rate': success_rate,
        'failure_rate': failure_rate,
    }
    return render(request, 'dashboard.html', context)

def Test(request):
    return render (request, 'test.html')

