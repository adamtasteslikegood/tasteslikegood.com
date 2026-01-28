"""
Stock image service for Unsplash API integration.

Provides intelligent image search with fallback strategies:
1. Use AI-generated keywords (best results)
2. Extract keywords from recipe description
3. Use recipe name
4. Use curated fallback images

All images are vegan-appropriate with proper attribution per Unsplash guidelines.
"""
import datetime
import requests
from config import UNSPLASH_ACCESS_KEY


# Curated static fallback images from Unsplash (real, permanent URLs)
FALLBACK_FOOD_IMAGES = [
    # All vegan-appropriate images for better fallback variety
    "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=800&h=600&fit=crop",  # Healthy bowl
    "https://images.unsplash.com/photo-1540189549336-e6e99c3679fe?w=800&h=600&fit=crop",  # Colorful salad
    "https://images.unsplash.com/photo-1512621776951-a57141f2eefd?w=800&h=600&fit=crop",  # Veggie bowl
    "https://images.unsplash.com/photo-1498837167922-ddd27525d352?w=800&h=600&fit=crop",  # Fresh produce
    "https://images.unsplash.com/photo-1543339308-43e59d6b73a6?w=800&h=600&fit=crop",  # Buddha bowl
    "https://images.unsplash.com/photo-1511690743698-d9d85f2fbf38?w=800&h=600&fit=crop",  # Avocado toast
    "https://images.unsplash.com/photo-1623428187969-5da2dcea5ebf?w=800&h=600&fit=crop",  # Smoothie bowl
    "https://images.unsplash.com/photo-1547592180-85f173990554?w=800&h=600&fit=crop",  # Vegetable curry
    "https://images.unsplash.com/photo-1574484284002-952d92456975?w=800&h=600&fit=crop",  # Indian dal
    "https://images.unsplash.com/photo-1559847844-5315695dadae?w=800&h=600&fit=crop",  # Hummus plate
    "https://images.unsplash.com/photo-1505576399279-565b52d4ac71?w=800&h=600&fit=crop",  # Fresh smoothie
    "https://images.unsplash.com/photo-1529059997568-3d847b1154f0?w=800&h=600&fit=crop",  # Grain bowl
    "https://images.unsplash.com/photo-1515543237350-b3eea1ec8082?w=800&h=600&fit=crop",  # Stuffed peppers
    "https://images.unsplash.com/photo-1600850056064-a8b380df8395?w=800&h=600&fit=crop",  # Falafel wrap
    "https://images.unsplash.com/photo-1544025162-d76694265947?w=800&h=600&fit=crop",  # Roasted veggies
    "https://images.unsplash.com/photo-1490914327627-9fe8d52f4d90?w=800&h=600&fit=crop",  # Green juice
]


def search_unsplash(keywords, per_page=1):
    """
    Search Unsplash for images matching keywords.

    Args:
        keywords: List of search terms or a single string
        per_page: Number of results to return (default 1)

    Returns:
        Dict with 'url' and 'attribution' info, or None if no results/error
        Attribution includes photographer name, profile URL, and Unsplash link
    """
    if not UNSPLASH_ACCESS_KEY:
        print("DEBUG: No Unsplash API key configured")
        return None

    # Join keywords if it's a list
    if isinstance(keywords, list):
        query = ' '.join(keywords[:3])  # Use first 3 keywords
    else:
        query = keywords

    # Add "vegan" to ensure food-appropriate results
    if 'vegan' not in query.lower():
        query = f"vegan {query}"

    # UTM params required by Unsplash API guidelines
    utm_params = "?utm_source=tasteslikegood&utm_medium=referral"

    try:
        response = requests.get(
            'https://api.unsplash.com/search/photos',
            params={
                'query': query,
                'per_page': per_page,
                'orientation': 'landscape',
                'content_filter': 'high',  # Safe content only
            },
            headers={
                'Authorization': f'Client-ID {UNSPLASH_ACCESS_KEY}'
            },
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            if data.get('results'):
                photo = data['results'][0]
                image_url = photo.get('urls', {}).get('regular')

                if image_url:
                    # Extract photographer info for attribution (required by Unsplash)
                    user = photo.get('user', {})
                    photographer_name = user.get('name', 'Unknown')
                    photographer_username = user.get('username', '')
                    photographer_url = f"https://unsplash.com/@{photographer_username}{utm_params}"
                    unsplash_url = f"https://unsplash.com{utm_params}"

                    print(f"DEBUG: Unsplash found image for '{query}' by {photographer_name}")

                    return {
                        'url': image_url,
                        'attribution': {
                            'photographer_name': photographer_name,
                            'photographer_url': photographer_url,
                            'unsplash_url': unsplash_url,
                            'html': f'Photo by <a href="{photographer_url}">{photographer_name}</a> on <a href="{unsplash_url}">Unsplash</a>'
                        }
                    }
        else:
            print(f"DEBUG: Unsplash API returned {response.status_code}: {response.text[:100]}")

    except Exception as e:
        print(f"DEBUG: Unsplash search failed: {e}")

    return None


def get_smart_stock_image(recipe_name, user_id='anonymous', description='', image_keywords=None):
    """
    Searches Unsplash for a stock image matching the recipe.

    Uses image_keywords (generated by Gemini with the recipe) for best results,
    falls back to description/name if keywords aren't available.

    Args:
        recipe_name: Name of the recipe
        user_id: User ID for metadata tracking
        description: Recipe description to extract keywords from
        image_keywords: AI-generated keywords for image search

    Returns:
        Tuple: (url, metadata_dict) or (fallback_url, metadata_dict) on failure.
        Metadata includes attribution info when from Unsplash (required by their API guidelines).
    """
    timestamp = datetime.datetime.now().isoformat()

    metadata = {
        'source': 'unsplash',
        'user_id': user_id,
        'search_query': '',
        'timestamp': timestamp,
        'success': False,
        'url_validated': False,
        'fallback_used': False,
        'attribution': None  # Will contain photographer credit for Unsplash images
    }

    try:
        # Strategy 1: Use image_keywords if provided (best option)
        if image_keywords and isinstance(image_keywords, list) and len(image_keywords) > 0:
            metadata['search_query'] = ' '.join(image_keywords[:3])
            result = search_unsplash(image_keywords)
            if result:
                metadata['success'] = True
                metadata['url_validated'] = True
                metadata['attribution'] = result['attribution']
                return result['url'], metadata

        # Strategy 2: Extract keywords from description
        if description:
            # Use first ~50 chars of description as search
            desc_keywords = description[:100].split()[:5]
            search_query = ' '.join(desc_keywords)
            metadata['search_query'] = search_query
            result = search_unsplash(search_query)
            if result:
                metadata['success'] = True
                metadata['url_validated'] = True
                metadata['attribution'] = result['attribution']
                return result['url'], metadata

        # Strategy 3: Use recipe name
        metadata['search_query'] = recipe_name
        result = search_unsplash(recipe_name)
        if result:
            metadata['success'] = True
            metadata['url_validated'] = True
            metadata['attribution'] = result['attribution']
            return result['url'], metadata

    except Exception as e:
        print(f"DEBUG: Unsplash search error: {e}")

    # Strategy 4: Curated fallback (no attribution needed - these are from Unsplash's free license)
    print(f"DEBUG: Using curated fallback for '{recipe_name}'")
    fallback_url = _get_fallback_image(recipe_name)
    metadata['success'] = True
    metadata['fallback_used'] = True
    metadata['url_validated'] = True
    return fallback_url, metadata


def _get_fallback_image(recipe_name):
    """
    Returns a deterministic fallback image based on recipe name.

    Uses a hash of the recipe name to consistently return the same image
    for the same recipe, avoiding random behavior.

    Args:
        recipe_name: Name of the recipe

    Returns:
        str: URL of the fallback image
    """
    # Use hash to get deterministic but varied selection
    name_hash = hash(recipe_name.lower())
    index = abs(name_hash) % len(FALLBACK_FOOD_IMAGES)
    return FALLBACK_FOOD_IMAGES[index]


def validate_image_url(url):
    """
    Validates that an image URL is accessible and returns an image.

    Args:
        url: URL to validate

    Returns:
        bool: True if the URL is valid and returns an image, False otherwise
    """
    # Common headers to mimic a browser request
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        # Try HEAD request first (faster, less bandwidth)
        response = requests.head(url, timeout=5, allow_redirects=True, headers=headers)

        # If HEAD succeeds with 200 and has image content-type, we're good
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            if content_type.startswith('image/'):
                return True

        # HEAD failed or no content-type - fall back to GET with stream
        # Many servers (Pexels, Unsplash) don't properly support HEAD requests
        response = requests.get(url, timeout=5, allow_redirects=True, headers=headers, stream=True)

        if response.status_code != 200:
            print(f"URL validation failed for {url}: status {response.status_code}")
            return False

        # Check content type is an image
        content_type = response.headers.get('Content-Type', '')
        if content_type.startswith('image/'):
            return True

        # Some servers don't set Content-Type properly, check if we can read image bytes
        # Read just the first few bytes to check for image magic numbers
        first_bytes = response.raw.read(16)
        response.close()

        # Check for common image magic numbers
        if (first_bytes.startswith(b'\xff\xd8\xff') or  # JPEG
            first_bytes.startswith(b'\x89PNG') or       # PNG
            first_bytes.startswith(b'GIF8') or          # GIF
            first_bytes.startswith(b'RIFF') or          # WebP
            first_bytes.startswith(b'<svg')):           # SVG
            return True

        print(f"URL validation failed for {url}: not an image (content-type: {content_type})")
        return False

    except Exception as e:
        print(f"URL validation failed for {url}: {e}")
        return False


def validate_and_refresh_stock_image(recipe_data, user_id='anonymous'):
    """
    Checks if existing stock image URL is valid, refreshes if not.

    Args:
        recipe_data: Recipe dictionary with stock_image_url and other fields
        user_id: User ID for metadata tracking

    Returns:
        Tuple: (updated_url, metadata_dict, was_refreshed)
            - updated_url: The validated or new image URL
            - metadata_dict: Image metadata (or None if not refreshed)
            - was_refreshed: Boolean indicating if URL was refreshed
    """
    current_url = recipe_data.get('stock_image_url')
    recipe_name = recipe_data.get('name', 'food')
    description = recipe_data.get('description', '')
    image_keywords = recipe_data.get('image_keywords', [])

    if current_url:
        # Validate existing URL
        is_valid = validate_image_url(current_url)
        if is_valid:
            # URL is working, no need to refresh
            return current_url, None, False
        else:
            print(f"Stock image URL invalid, refreshing for {recipe_name}")

    # Need to get a new URL - pass keywords, description for better image matching
    new_url, metadata = get_smart_stock_image(recipe_name, user_id, description, image_keywords)
    return new_url, metadata, True
