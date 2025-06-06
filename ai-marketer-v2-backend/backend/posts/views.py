import json
import logging
from datetime import datetime
from itertools import chain

from celery.result import AsyncResult
from django.contrib.auth import get_user_model
from django.utils import timezone
from PIL import Image
import requests
from rest_framework import status
from rest_framework.generics import ListCreateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from businesses.models import Business
from config.celeryTasks import publish_to_meta_task,publishToMeta
from config.constants import POST_CATEGORIES_OPTIONS, SOCIAL_PLATFORMS
from posts.models import Post, Category
from posts.serializers import PostSerializer
from promotions.models import Promotion
from social.models import SocialMedia
from utils.discord_api import upload_image_file_to_discord
from utils.meta_api import (
    get_facebook_page_id,
    get_user_access_token,
    sync_posts_from_meta
)
from utils.square_api import get_square_menu_items

User = get_user_model()
logger = logging.getLogger(__name__)

class PostListCreateView(ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = PostSerializer

    def get_queryset(self):
        business = Business.objects.filter(owner=self.request.user).first()
        if not business:
            return Post.objects.none()
        
        linked_platforms_queryset = SocialMedia.objects.filter(business=business)
        linked_platforms = [
            {
                "key": linked_platform.platform,
                "label": next(
                    (p["label"] for p in SOCIAL_PLATFORMS if p["key"] == linked_platform.platform),
                    linked_platform.platform
                ),
            }
            for linked_platform in linked_platforms_queryset
        ]

        if len(linked_platforms) == 0:
            return Post.objects.none()

        self.sync_errors = []
        if any(platform["key"] == "facebook" for platform in linked_platforms):
            try:
                result = sync_posts_from_meta(self.request.user.id, business, 'facebook')
                if isinstance(result, dict) and result.get("status") == False:
                    logger.error(f"Error syncing Facebook posts: {result.get('error')}")
                    self.sync_errors.append({"platform": "facebook", "error": result.get("error")})
            except Exception as e:
                logger.error(f"Error syncing Facebook posts: {e}")
                self.sync_errors.append({"platform": "facebook", "error": str(e)})
        
        if any(platform["key"] == "instagram" for platform in linked_platforms):
            try:
                result = sync_posts_from_meta(self.request.user.id, business, 'instagram')
                if isinstance(result, dict) and result.get("status") == False:
                    logger.error(f"Error syncing Instagram posts: {result.get('error')}")
                    self.sync_errors.append({"platform": "instagram", "error": result.get("error")})
            except Exception as e:
                logger.error(f"Error syncing Instagram posts: {e}")
                self.sync_errors.append({"platform": "instagram", "error": str(e)})

        failed_posts = list(Post.objects.filter(
            business=business,
            status='Failed'
        ).order_by('-created_at'))

        scheduled_posts = list(Post.objects.filter(
            business=business,
            status='Scheduled'
        ).order_by('-scheduled_at'))

        posted_posts = list(Post.objects.filter(
            business=business,
            status='Published'
        ).order_by('-posted_at'))

        combined_posts = list(chain(failed_posts, scheduled_posts, posted_posts))
        return combined_posts

    def list(self, request, *args, **kwargs):
        business = Business.objects.filter(owner=self.request.user).first()
        linked_platforms_queryset = SocialMedia.objects.filter(business=business)
        linked_platforms = [
            {
                "key": linked_platform.platform,
                "label": next(
                    (p["label"] for p in SOCIAL_PLATFORMS if p["key"] == linked_platform.platform),
                    linked_platform.platform
                ),
            }
            for linked_platform in linked_platforms_queryset
        ]
        linked = len(linked_platforms) > 0

        queryset = self.get_queryset()
        response_data = self.get_serializer(queryset, many=True).data
        response = {"linked": linked, "posts": response_data}

        if hasattr(self, 'sync_errors') and self.sync_errors:
            response["sync_errors"] = self.sync_errors

        return Response(response, status=status.HTTP_200_OK)
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def crop_center_resize(self, image, target_width=1080, target_height=1350):
        aspect_target = target_width / target_height
        width, height = image.size
        aspect_original = width / height

        #Crop to match aspect ratio
        if aspect_original > aspect_target:
            #If too wide — crop sides
            new_width = int(height * aspect_target)
            left = (width - new_width) // 2
            right = left + new_width
            top, bottom = 0, height
        else:
            #If too tall — crop top/bottom
            new_height = int(width / aspect_target)
            top = (height - new_height) // 2
            bottom = top + new_height
            left, right = 0, width

        cropped = image.crop((left, top, right, bottom))
        resized = cropped.resize((target_width, target_height), Image.LANCZOS)
        return resized

    def upload_image_file(self,image_file,aspectRatio):
        img = Image.open(image_file)
        if(aspectRatio == "1/1"):
            img = self.crop_center_resize(img,1080,1080) # 1:1 portrait
        else:
            img = self.crop_center_resize(img) # 4:5 portrait
        image_url = upload_image_file_to_discord(img)['image_url']
        return image_url

    def get(self, request, *args, **kwargs):
        if request.query_params.get('create') == 'true':
            business = Business.objects.filter(owner=request.user).first()

            if not business:
                return Response({"error": "Business not found"}, status=404)

            selectable_categories = [
                {"id": index + 1, "label": category["label"], "is_selected": False}
                for index, category in enumerate(POST_CATEGORIES_OPTIONS)
            ]

            linked_platforms_queryset = SocialMedia.objects.filter(business=business)
            linked_platforms = [
                {
                    "key": linked_platform.platform,
                    "label": next(
                        (p["label"] for p in SOCIAL_PLATFORMS if p["key"] == linked_platform.platform),
                        linked_platform.platform
                    ),
                }
                for linked_platform in linked_platforms_queryset
            ]

            square_integration_status = {
                "square_connected": False,
                "items": [],
                }
            
            try: 
                square_integration_status = get_square_menu_items(business)
            except Exception as e:
                logger.error(f"Error checking Square integration: {e}")

            response_data = {
                "business": {
                    "target_customers": business.target_customers,
                    "vibe": business.vibe,
                    "square_connected": square_integration_status["square_connected"],
                    "items": square_integration_status["items"],
                },
                "selectable_categories": selectable_categories,
                "linked_platforms": linked_platforms,
            }

            return Response(response_data)

        return self.list(request, *args, **kwargs)
    
    def post(self, request):
        business = Business.objects.filter(owner=request.user).first()
        if not business:
            return Response({"error": "Business not found"}, status=status.HTTP_404_NOT_FOUND)
        
        # Handle file upload
        if 'image' not in request.FILES:
            return Response({"error": "No Image provided"}, status=status.HTTP_400_BAD_REQUEST)
        
        data = request.POST

        try:
            platform = SocialMedia.objects.filter(business=business, platform=data["platform"]).first()
            if not platform:
                return Response({"error": f"No connected account found for {data['platform']}"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        
        promotion = None
        if "promotion" in data and data["promotion"]:
            try:
                promotion = Promotion.objects.get(id=data["promotion"])
            except Promotion.DoesNotExist:
                return Response({"error": "Invalid promotion ID"}, status=status.HTTP_400_BAD_REQUEST)
        
        scheduled_at = data.get("scheduled_at")
        if scheduled_at:
            posted_at = None
            link = None
            post_status = "Scheduled"
        else :
            scheduled_at = None
            posted_at = timezone.now()
            post_status = "Published"

        image_url=self.upload_image_file(request.FILES.get('image'),data.get("aspect_ratio","4/5"))
        access_token = get_user_access_token(request.user.id)
        scheduled_id=None

        match data["platform"]:
            case 'facebook':
                #Schedule post
                if scheduled_at:
                    dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
                    result = publish_to_meta_task.apply_async(args=["facebook", data.get("caption", ""),image_url,access_token],eta=dt)
                    scheduled_id=result.id
                    link="Not published yet!"
                #Else post straight away
                else:
                    response = publishToMeta("facebook", data.get("caption", ""),image_url,access_token)
                    if (response.get("status") == False):
                        return Response({"error": response.get("error")}, status=status.HTTP_400_BAD_REQUEST)   #Then no post id was provided
                    link=response.get("message")
            case 'instagram':
                #Schedule post
                if scheduled_at:
                    dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
                    result = publish_to_meta_task.apply_async(args=["instagram", data.get("caption", ""),image_url,access_token],eta=dt)
                    scheduled_id=result.id
                    link="Not published yet!"
                #Else post straight away
                else:
                    response = publishToMeta("instagram", data.get("caption", ""),image_url,access_token)
                    if (response.get("status") == False):
                        return Response({"error": response.get("error")}, status=status.HTTP_400_BAD_REQUEST)   #Then no post id was provided
                    link=response.get("message")
            case _:
                return Response({"error": "Invalid platform"}, status=status.HTTP_400_BAD_REQUEST)
        
        logger.error(scheduled_id)
        #Now create post object on backend here if successfully published/scheduled
        post = Post.objects.create(
            business=business,
            platform=platform,
            caption=data.get("caption", ""),
            image=image_url,
            link=link,
            posted_at=posted_at,
            scheduled_at=scheduled_at,
            status=post_status,
            promotion=promotion,
            scheduled_id=scheduled_id
        )
                
        categories_data = json.loads(data.get("categories", "[]"))
        categories = Category.objects.filter(id__in=categories_data)
        post.categories.set(categories)

        return Response({"message": "Post created successfully!"}, status=status.HTTP_201_CREATED)


class PostDetailView(APIView):
    """
    API view for retrieving, updating and deleting a specific post.
    """
    permission_classes = [IsAuthenticated]

    def get_post(self, pk, user):
        """Helper method to get a post and verify ownership"""
        business = Business.objects.filter(owner=user).first()
        if not business:
            return None, Response({"error": "Business not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            post = Post.objects.get(pk=pk, business=business)
            return post, None
        except Post.DoesNotExist:
            return None, Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)
        
    def get_meta_comments(self, user_id, platform, post_id):
        user = User.objects.get(id=user_id)
        #Get Access Token
        token_decoded = get_user_access_token(user_id)
        #Get Facebook page id
        facebookPageID = get_facebook_page_id(token_decoded)

        if not facebookPageID:
            return {"error": "Unable to retrieve Facebook Page ID! Maybe reconnect your Facebook or Instagram account in Settings!", "status": False}
        
        #For Facebook
        if platform == 'facebook':
            #Get page access token
            url = f'https://graph.facebook.com/v22.0/me/accounts?access_token={token_decoded}'
            response = requests.get(url)
            if response.status_code != 200:
                # Handle error response
                return {"error": f"Unable to retrieve page access token. {response.text}", "status": False}
            metasData = response.json()
            if not metasData.get("data"):
                return {"error": "Unable to retrieve page access token 2", "status": False}
            #Get the page access token
            page_access_token = metasData.get("data")[0]["access_token"]
            url = f'https://graph.facebook.com/v22.0/{post_id}/comments?access_token={page_access_token}'
            response = requests.get(url)
            if response.status_code != 200:
                # Handle error response
                return {"error": f"Unable to fetch posts. {response.text}", "status": False}
            media_data = response.json()
            if not media_data.get("data"):
                return {"error": f"Unable to retrieve posts {response.text}", "status": False}
            posts_data = media_data.get("data")

            #Format into proper array
            arr=[]
            for comment in posts_data:
                if comment.get('message'):
                    replies = self.get_comment_replies(user, platform, comment['id'], page_access_token)
                    comment_data= self.get_comment_likes(platform, comment['id'], page_access_token)
                    arr.append({'id':comment['id'],'createdTime':comment['created_time'],'from':{'name':comment['from']['name']},'message':comment['message'],'replies':replies,'likeCount':comment_data['count'],'selfLike':comment_data['self_like']})
            logger.error(arr)
            return {"message": arr, "status": True}
        
        #For Insta
        elif platform == 'instagram':
            url = f'https://graph.facebook.com/v22.0/{post_id}/comments?access_token={token_decoded}'
            response = requests.get(url)
            if response.status_code != 200:
                # Handle error response
                return {"error": f"Unable to fetch posts. {response.text}", "status": False}
            media_data = response.json()
            if not media_data.get("data"):
                return {"error": f"Unable to retrieve posts {response.text}", "status": False}
            posts_data = media_data.get("data")

            #Format into proper array
            arr=[]
            for comment in posts_data:
                if comment.get('text'):
                    replies = self.get_comment_replies(user, platform,comment['id'], token_decoded)
                    arr.append({'id':comment['id'],'createdTime':comment['timestamp'],'from':{'name':'User'},'message':comment['text'],'replies':replies,'like_count':None,'self_like':False})
            logger.error(arr)
            return {"message": arr, "status": True}
        
    def get_comment_likes(self,platform,comment_id,token_decoded):
        if platform=='facebook':
            #Get replies
            url = f'https://graph.facebook.com/v22.0/{comment_id}/likes?summary=true&access_token={token_decoded}'
            response = requests.get(url)
            if response.status_code != 200:
                # Handle error response
                logger.error(f"Error fetching comments likes {response.text}")
                return {'count':None,'self_like':False}
            media_data = response.json()
            if not media_data.get("summary"):
                logger.error(f"Error retrieving comments likes {response.text}")
                return {'count':0,'self_like':False}
            posts_data = media_data.get("summary")
            
            return {'count':posts_data['total_count'],'self_like':posts_data['has_liked']}
        
    def get_comment_replies(self,user,platform,comment_id,token_decoded):
        #Get account username
        business = Business.objects.filter(owner=user).first()
        linked_platform = SocialMedia.objects.filter(business=business, platform=platform)
        if(not linked_platform.exists()):
            return []
        
        if(platform=='facebook'):
            #Get replies
            url = f'https://graph.facebook.com/v22.0/{comment_id}/comments?access_token={token_decoded}'
            response = requests.get(url)
            if response.status_code != 200:
                # Handle error response
                logger.error(f"Error fetching comments {response.text}")
                return []
            media_data = response.json()
            if not media_data.get("data"):
                logger.error(f"Error retrieving comments {response.text}")
                return []
            posts_data = media_data.get("data")

            replies=[]
            for comment in posts_data:
                if comment.get('message'):
                    #Get only replies from yourself
                    if (comment['from']['name']==linked_platform.first().username):
                        replies.append(comment.get('message'))

            return replies
        elif platform=='instagram':
            #Get replies
            url = f'https://graph.facebook.com/v22.0/{comment_id}/replies?access_token={token_decoded}'
            response = requests.get(url)
            if response.status_code != 200:
                # Handle error response
                logger.error(f"Error fetching comments {response.text}")
                return []
            media_data = response.json()
            if not media_data.get("data"):
                logger.error(f"Error retrieving comments {response.text}")
                return []
            posts_data = media_data.get("data")

            replies=[]
            for comment in posts_data:
                if comment.get('text'):
                    replies.append(comment.get('text'))

            return replies
        return []
    
    def post_comment_likes(self, platform, comment_id, user_id):
        #Get Access Token
        token_decoded = get_user_access_token(user_id)
        #Get Facebook page id
        facebookPageID = get_facebook_page_id(token_decoded)
        if not facebookPageID:
            return {"error": "Unable to retrieve Facebook Page ID! Maybe reconnect your Facebook or Instagram account in Settings!", "status": False}
        
        if platform=='facebook':
            #Get page access token
            url = f'https://graph.facebook.com/v22.0/me/accounts?access_token={token_decoded}'
            response = requests.get(url)
            if response.status_code != 200:
                # Handle error response
                return {"error": f"Unable to retrieve page access token. {response.text}", "status": False}
            metasData = response.json()
            if not metasData.get("data"):
                return {"error": "Unable to retrieve page access token 2", "status": False}
            #Get the page access token
            page_access_token = metasData.get("data")[0]["access_token"]

            like_obj=self.get_comment_likes('facebook',comment_id,page_access_token)
            logger.error(like_obj)
            if( not like_obj['self_like']):
                #Then leave a like
                url = f'https://graph.facebook.com/v22.0/{comment_id}/likes?access_token={page_access_token}'
                response = requests.post(url)
                if response.status_code != 200:
                    # Handle error response
                    logger.error(f"Error liking comment {response.text}")
                    return False
                media_data = response.json()
                logger.error(media_data)
            else:
                #Then delete
                url = f'https://graph.facebook.com/v22.0/{comment_id}/likes?access_token={page_access_token}'
                response = requests.delete(url)
                if response.status_code != 200:
                    logger.error(f"Error liking comment {response.text}")
                    return False
                media_data = response.json()
                logger.error(media_data)
            return True

    def post_comment_reply(self, platform, comment_id, user_id, msg):
        #Get Access Token
        token_decoded = get_user_access_token(user_id)
        #Get Facebook page id
        facebookPageID = get_facebook_page_id(token_decoded)
        if not facebookPageID:
            return {"error": "Unable to retrieve Facebook Page ID! Maybe reconnect your Facebook or Instagram account in Settings!", "status": False}
        
        #Get page access token
        url = f'https://graph.facebook.com/v22.0/me/accounts?access_token={token_decoded}'
        response = requests.get(url)
        if response.status_code != 200:
            # Handle error response
            return {"error": f"Unable to retrieve page access token. {response.text}", "status": False}
        metasData = response.json()
        if not metasData.get("data"):
            return {"error": "Unable to retrieve page access token 2", "status": False}
        #Get the page access token
        page_access_token = metasData.get("data")[0]["access_token"]

        if platform=='facebook' and msg!="delete000":
            #Now Reply
            url = f'https://graph.facebook.com/v22.0/{comment_id}/comments?message={msg}&access_token={page_access_token}'
            response = requests.post(url)
            if response.status_code != 200:
                # Handle error response
                logger.error(f"Error replying to comment {response.text}")
                return False
                #return {"error": f"Error liking comment. {response.text}", "status": False}
            media_data = response.json()
            logger.error(media_data)
            return True
        elif platform=="facebook":
            #Now Delete
            url = f'https://graph.facebook.com/v22.0/{comment_id}?access_token={page_access_token}'
            response = requests.delete(url)
            if response.status_code != 200:
                # Handle error response
                logger.error(f"Error deleting comment {response.text}")
                return False
                #return {"error": f"Error liking comment. {response.text}", "status": False}
            media_data = response.json()
            logger.error(media_data)
            return True

    def get(self, request, pk,msg=""):
        """Check to see if its a comment operation"""
        if 'likecomments' in request.path:
            return Response({"message": self.post_comment_likes('facebook', pk, request.user.id)}, status=200)
        if 'replycomments' in request.path:
            return Response({"message": self.post_comment_reply('facebook', pk, request.user.id, msg)}, status=200)
        
        """Retrieve a specific post"""
        post, error_response = self.get_post(pk, request.user)
        if 'comments' in request.path:
            return Response({"message": self.get_meta_comments(request.user.id, post.platform.platform,post.post_id)}, status=200)

        if error_response:
            return error_response

        serializer = PostSerializer(post)
        return Response(serializer.data)
    
    def upload_image_file(self,image_file,aspectRatio):
        img = Image.open(image_file)
        if(aspectRatio == "1/1"):
            img = self.crop_center_resize(img,1080,1080) # 1:1 portrait
        else:
            img = self.crop_center_resize(img) # 4:5 portrait
        image_url = upload_image_file_to_discord(img)['image_url']
        return image_url

    def crop_center_resize(self, image, target_width=1080, target_height=1350):
            aspect_target = target_width / target_height
            width, height = image.size
            aspect_original = width / height

            #Crop to match aspect ratio
            if aspect_original > aspect_target:
                #If too wide — crop sides
                new_width = int(height * aspect_target)
                left = (width - new_width) // 2
                right = left + new_width
                top, bottom = 0, height
            else:
                #If too tall — crop top/bottom
                new_height = int(width / aspect_target)
                top = (height - new_height) // 2
                bottom = top + new_height
                left, right = 0, width

            cropped = image.crop((left, top, right, bottom))
            resized = cropped.resize((target_width, target_height), Image.LANCZOS)
            return resized

    def patch(self, request, pk):
        """Update a post partially"""
        post, error_response = self.get_post(pk, request.user)
        if error_response:
            return error_response
        
        if post.scheduled_id:
            AsyncResult(post.scheduled_id).revoke(terminate=True)

        # Handle caption updates
        if 'caption' in request.data:
            post.caption = request.data['caption']

        # Handle categories updates
        if 'categories' in request.data:
            # First clear existing categories
            post.categories.clear()
            # Then add new categories
            category_labels = request.data.getlist('categories')
            for label in category_labels:
                try:
                    category = Category.objects.get(label=label)
                except Category.DoesNotExist:
                    return Response({"error": f"Category '{label}' does not exist."}, status=400)
                post.categories.add(category)

        # Handle image updates if provided
        image_url=None
        if 'image' in request.FILES:
            image_url=self.upload_image_file(request.FILES['image'], request.data.get("aspect_ratio","4/5"))
            post.image = image_url
        else:
            image_url = post.image
        
        access_token = get_user_access_token(request.user.id)

        #Check if its a retry initiated by user
        if request.data.get('retry'):
            logger.error("Retry detected")
            match post.platform.platform:
                case 'facebook':
                    response = publishToMeta("facebook", request.data.get("caption", ""),image_url,access_token)
                    if (response.get("status") == False):
                        return Response({"error": response.get("error")}, status=status.HTTP_400_BAD_REQUEST)   #Then no post id was provided
                    link=response.get("message")
                case 'instagram':
                    response = publishToMeta("instagram", request.data.get("caption", ""),image_url,access_token)
                    if (response.get("status") == False):
                        return Response({"error": response.get("error")}, status=status.HTTP_400_BAD_REQUEST)   #Then no post id was provided
                    link=response.get("message")
                case _:
                    return Response({"error": "Invalid platform"}, status=status.HTTP_400_BAD_REQUEST)
            post.link = link
            post.scheduled_id=None
            post.status = 'Published'
            post.posted_at = timezone.now()
            post.save()
            return Response({"message": "Retry successful"}, status=status.HTTP_200_OK)

        # Handle scheduled_at updates
        if 'scheduled_at' in request.data:
            scheduled_at = request.data.get("scheduled_at")

            if scheduled_at:
                post.scheduled_at = scheduled_at
                post.status = 'Scheduled'
                
                #Send new task to celery for publishing
                dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
                match post.platform.platform:
                    case 'facebook':
                        result = publish_to_meta_task.apply_async(args=["facebook", request.data.get("caption", ""),image_url,access_token],eta=dt)                     
                    case 'instagram':    
                        result = publish_to_meta_task.apply_async(args=["instagram", request.data.get("caption", ""),image_url,access_token],eta=dt)
                    case _:
                        return Response({"error": "Invalid platform"}, status=status.HTTP_400_BAD_REQUEST)
                scheduled_id=result.id
                post.scheduled_id=scheduled_id
            else:
                post.scheduled_at = None
                try:
                    match post.platform.platform:
                        case 'facebook':
                            response = publishToMeta("facebook", request.data.get("caption", ""),image_url,access_token)
                            if (response.get("status") == False):
                                return Response({"error": response.get("error")}, status=status.HTTP_400_BAD_REQUEST)   #Then no post id was provided
                            link=response.get("message")
                        case 'instagram':
                            response = publishToMeta("instagram", request.data.get("caption", ""),image_url,access_token)
                            if (response.get("status") == False):
                                return Response({"error": response.get("error")}, status=status.HTTP_400_BAD_REQUEST)   #Then no post id was provided
                            link=response.get("message")
                        case _:
                            return Response({"error": "Invalid platform"}, status=status.HTTP_400_BAD_REQUEST)
                    success = True
                    post.link = link
                    post.scheduled_id=None

                    if success:
                        post.status = 'Published'
                        post.posted_at = timezone.now()
                    else:
                        post.status = 'Failed'
                except Exception as e:
                    logger.error(f"Error publishing post: {e}")
                    post.status = 'Failed'

        post.save()

        serializer = PostSerializer(post)
        return Response(serializer.data)
    
    def delete_facebook(self,token_decoded,post_id):
        #Get page access token
        url = f'https://graph.facebook.com/v22.0/me/accounts?access_token={token_decoded}'
        response = requests.get(url)
        if response.status_code != 200:
            # Handle error response
            return {"error": f"Unable to retrieve page access token. {response.text}", "status": False}
        metasData = response.json()
        if not metasData.get("data"):
            return {"error": "Unable to retrieve page access token 2", "status": False}
        #Get the page access token
        page_access_token = metasData.get("data")[0]["access_token"]

        url = f'https://graph.facebook.com/v22.0/{post_id}?access_token={page_access_token}'
        response=requests.delete(url)
        if response.status_code != 200:
            # Handle error response
            return {"error": f"Unable to delete post. {response.text}", "status": False}
        metasData = response.json()
        if not metasData.get("success"):
            return {"error": "Unable to retrieve post deletion status", "status": False}
        return {"message": metasData.get("success"), "status": True}

    def delete(self, request, pk):
        """Delete a post"""
        post, error_response = self.get_post(pk, request.user)

        if post.posted_at and post.scheduled_id:
            AsyncResult(post.scheduled_id).revoke(terminate=True)

        if error_response:
            return error_response

        if(post.status!="Published"):
            post.delete()
            return Response({"message": "Post deleted successfully"}, status=status.HTTP_200_OK)

        if post.platform.platform=='instagram':
            return Response({"message": "Instagram deletion not implemented yet"}, status=status.HTTP_400_BAD_REQUEST)
        
        delete_message = self.delete_facebook(get_user_access_token(request.user.id),post.post_id)
        if(delete_message['status'] == False):
            return Response({"message": "Some Error Deleting Post"}, status=status.HTTP_400_BAD_REQUEST)

        post.delete()
        return Response({"message": "Post deleted successfully"}, status=status.HTTP_200_OK)
