
from pydantic import BaseModel


class InstagramPost(BaseModel):
    shortcode: str
    url: str
    image_url: str
    caption: str
    likes: int
    timestamp: str
    location_name: str | None = None
    username: str
    post_category: str = "all"  # "restaurants" or "things_to_do"
    lat: float | None = None
    lon: float | None = None


class YelpBusiness(BaseModel):
    id: str
    name: str
    url: str
    rating: float
    review_count: int
    price: str | None = None
    categories: list[str]
    address: str
    image_url: str | None = None
    lat: float | None = None
    lon: float | None = None


class FoursquarePlace(BaseModel):
    fsq_id: str
    name: str
    categories: list[str]
    address: str | None = None
    distance: int | None = None
    link: str | None = None
    lat: float | None = None
    lon: float | None = None


class PlacesResponse(BaseModel):
    location: str
    category: str
    location_lat: float | None = None
    location_lon: float | None = None
    yelp_businesses: list[YelpBusiness]
    osm_restaurants: list[FoursquarePlace]
    osm_activities: list[FoursquarePlace]
    warnings: list[str] = []


class SearchResponse(BaseModel):
    location: str
    category: str
    location_lat: float | None = None
    location_lon: float | None = None
    instagram_posts: list[InstagramPost]
    yelp_businesses: list[YelpBusiness]
    osm_restaurants: list[FoursquarePlace]
    osm_activities: list[FoursquarePlace]
    warnings: list[str] = []
