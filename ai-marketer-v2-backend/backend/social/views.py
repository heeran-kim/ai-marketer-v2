# backend/social/views.py
import logging
import requests

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import transaction
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from businesses.models import Business

from .models import SocialMedia
from .serializers import SocialMediaSerializer
from utils.meta_api import get_facebook_data, sync_posts_from_meta

TWOFA_ENCRYPTION_KEY = settings.TWOFA_ENCRYPTION_KEY
FACEBOOK_APP_ID = settings.FACEBOOK_APP_ID
FACEBOOK_SECRET = settings.FACEBOOK_SECRET
FACEBOOK_REDIRECT_URI = settings.FACEBOOK_REDIRECT_URI

logger = logging.getLogger(__name__)

class LinkedSocialAccountsView(APIView):
    """
    View to retrieve all linked social accounts for the current user.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        business = Business.objects.filter(owner=request.user).first()
        linked_platforms_queryset = SocialMedia.objects.filter(business=business)
        serialized_data = SocialMediaSerializer(linked_platforms_queryset, many=True).data

        return Response(serialized_data, status=status.HTTP_200_OK)

class ConnectSocialAccountView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request, provider):
        business = Business.objects.filter(owner=request.user).first()
        other_meta_platform = SocialMedia.objects.filter(business=business, platform="instagram" if provider=="facebook" else "facebook") #check to see if the other meta social account is linked
        if provider=="instagram" or provider=="facebook":
            client_id = FACEBOOK_APP_ID
            redirect_uri = f'{FACEBOOK_REDIRECT_URI}{provider}'
            scope = 'business_management,pages_show_list,pages_read_user_content,pages_read_engagement,pages_manage_engagement,pages_manage_posts,instagram_basic,instagram_content_publish' if other_meta_platform.exists() else('business_management,instagram_basic,instagram_content_publish' if provider=="instagram" else 'business_management,pages_show_list,pages_read_user_content,pages_read_engagement,pages_manage_engagement,pages_manage_posts')
            login_url = f"https://www.facebook.com/v22.0/dialog/oauth?client_id={client_id}&redirect_uri={redirect_uri}&scope={scope}&response_type=code"
            return Response({'link':login_url},status=status.HTTP_200_OK)

        return Response({"message": "OAuth initiation is not yet implemented."}, status=status.HTTP_501_NOT_IMPLEMENTED)
    
class FinalizeOauthView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get_access_token(self,code,provider,user):
        #Get Meta's access token
        app_id = FACEBOOK_APP_ID
        app_secret = FACEBOOK_SECRET
        redirect_uri = f'{FACEBOOK_REDIRECT_URI}{provider}'

        url = 'https://graph.facebook.com/v22.0/oauth/access_token'
        params = {
            'client_id': app_id,
            'client_secret': app_secret,
            'redirect_uri': redirect_uri,
            'code': code,
        }

        response = requests.get(url, params=params)
        if response.status_code == 200:
            # Successful exchange, get the access token
            data = response.json()
            #Encrypt the access token
            secret_bytes = str.encode(data['access_token']) #convert to bytes
            f = Fernet(TWOFA_ENCRYPTION_KEY) #assign encryption key
            secret_encrypted = f.encrypt(secret_bytes) 
            # Save the encrypted access token to the user model
            user.access_token=secret_encrypted
            user.save() #save it to the user database
            return data['access_token']
        else:
            return None #return none if error fetching access token

    def get_instagram_data(self, access_token, metasData):
        #get the Instagram account ID
        url = f'https://graph.facebook.com/v22.0/{metasData["id"]}?fields=instagram_business_account&access_token={access_token}'
        
        response = requests.get(url)
        data=response.json()
        if response.status_code != 200:
            return None
        
        insta_account_data = data.get("instagram_business_account")
        if not insta_account_data:
            return None
        
        #get the Instagram account Name
        url = f'https://graph.facebook.com/v22.0/{data["instagram_business_account"]["id"]}?fields=username&access_token={access_token}'
        
        response = requests.get(url)
        data=response.json()
        if response.status_code != 200:
            return None
        
        instagram_account=data["username"]
        instagram_link=f'https://www.instagram.com/{data["username"]}/'

        return instagram_account,instagram_link

    def save_to_db(self, provider, user, metasData, instagram_account=None, instagram_link=None):
        # Now save the updated social media account to the database
        with transaction.atomic():
            business = Business.objects.filter(owner=user).first()
            if not business:
                return False
            
            SocialMedia.objects.filter(business=business, platform=provider).delete()
            
            # Create a new SocialMedia instance
            social_media = SocialMedia.objects.create(
                business=business,
                platform=provider,
                username=metasData["name"] if provider=="facebook" else instagram_account,
                link=f'https://www.facebook.com/{metasData["id"]}' if provider=="facebook" else instagram_link,
            )

    def post(self, request):
        code=request.data.get('code')
        provider=request.data.get('provider')
        if not code:
            return Response({'message': 'No Oauth Code provided!'}, status=status.HTTP_400_BAD_REQUEST)
        
        business = Business.objects.filter(owner=request.user).first()
        if not business:
            return Response({"error": "Business not found"}, status=status.HTTP_404_NOT_FOUND)
        
        access_token = self.get_access_token(code, provider, request.user)
        if not access_token:
            return Response({'message': 'No access token found!'}, status=status.HTTP_400_BAD_REQUEST)
        
        #For retriving the Facebook page id
        facebook_data = get_facebook_data(access_token)
        if not facebook_data:
            return Response({'message': 'No Facebook page found!'}, status=status.HTTP_400_BAD_REQUEST)

        #For retriving the Instagram account
        instagram_account=None
        instagram_link=None
        if provider == "instagram":
            insta_data = self.get_instagram_data(access_token, facebook_data)
            if not insta_data:
                return Response({'message': 'No Instagram account found!'}, status=status.HTTP_400_BAD_REQUEST)
            instagram_account=insta_data[0]
            instagram_link=insta_data[1]
        
        try:
            self.save_to_db(provider, request.user, facebook_data, instagram_account, instagram_link)
            try:
                result = sync_posts_from_meta(request.user.id, business, provider)
                if isinstance(result, dict) and result.get("status") == False:
                    logger.error(f"Error syncing posts: {result.get('error')}")
            except Exception as e:
                logger.error(f"Error syncing posts: {e}")
        except:
            return Response({'message': 'Error saving to database! Make sure you have a business created first!'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        
        return Response({'message': 'Successfully linked!'}, status=status.HTTP_200_OK)

class OAuthCallbackView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request,provider):
        return Response({"message": "OAuth callback handling is not yet implemented."}, status=status.HTTP_501_NOT_IMPLEMENTED)

class DisconnectSocialAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, provider):
        business = Business.objects.filter(owner=request.user).first()
        if not business:
            return Response({"error": "Business not found."}, status=status.HTTP_404_NOT_FOUND)

        social_account = SocialMedia.objects.filter(business=business, platform=provider).first()
        if not social_account:
            return Response({"error": f"No connected account found for provider '{provider}'"}, status=status.HTTP_404_NOT_FOUND)

        social_account.delete()
        return Response({"message": f"Disconnected from {provider} successfully"}, status=status.HTTP_200_OK)

