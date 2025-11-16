from django.urls import path
from . import views
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
  
    path('upload/', views.upload_view, name='upload'),
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('campaign/<int:campaign_id>/', views.campaign_detail_view, name='campaign_detail'),
    path('test/',views.Test,name='test')



]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    
