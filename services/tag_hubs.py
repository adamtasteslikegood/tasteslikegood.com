"""Curated tag hub pages: ``/browse/tag/<slug>`` (KAN-274).

Category-shaped queries ("vegan dinner ideas", "vegan breakfast recipes",
"high protein vegan recipes") cannot be won by a single recipe page; they need
a page that gathers the recipes and says what they are. These hubs are that
page, one per allow-listed category. Arbitrary tag filters are deliberately not
hubs: Sprint 10 decided filtered /browse views stay canonical to /browse, and a
curated hub is the indexable exception.

Membership is computed live from recipe tags, so the hubs track the catalog
without anyone editing this file. The intros are written once, by hand, and
describe the kinds of dishes each hub holds rather than counting them.

Kept in the Backend (not next to the cookbook's specs/canonical-recipes.json)
because Flask renders the hubs and cannot read the cookbook repo at runtime.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

# A hub with fewer recipes than this is served ``noindex`` and left out of the
# sitemap and every hub link: a two-card "category" is a thin page.
MIN_INDEXABLE_RECIPES = 3


@dataclass(frozen=True)
class TagHub:
    slug: str
    title: str
    # Short name for the site footer (KAN-319), where ``title`` would repeat
    # "Vegan … Recipes" twelve times. The cookbook's src/site-nav.json mirrors it.
    label: str
    aliases: frozenset[str]
    intro: str


def normalize_tag(tag: str) -> str:
    """``"Gluten-Free "`` → ``"gluten free"``: case, hyphens and spacing folded."""
    return re.sub(r"\s+", " ", tag.replace("-", " ")).strip().lower()


def _hub(slug: str, title: str, label: str, aliases: Iterable[str], intro: str) -> TagHub:
    return TagHub(
        slug,
        title,
        label,
        frozenset(normalize_tag(a) for a in aliases),
        " ".join(intro.split()),
    )


# Order is the order hubs are listed on /browse and the order a recipe's
# breadcrumb picks its category from (first hub it belongs to).
TAG_HUBS: tuple[TagHub, ...] = (
    _hub(
        "breakfast",
        "Vegan Breakfast Recipes",
        "Breakfast",
        ["breakfast", "brunch"],
        """
        Plant-based breakfasts that are worth getting up for: stacks of pancakes, biscuits
        smothered in mushroom gravy, breakfast burritos, huevos rancheros with a tofu
        scramble, muffins, and a full English fry-up. Some are quick weekday plates and some
        are weekend brunch projects. Every recipe lists its prep and cook time up top, so
        you can tell which is which before you start, and recipe cards show a photo of the
        finished dish when one is available. There are no eggs, dairy or bacon in any of
        them; tofu, beans, oats and a well-stocked spice rack do the work.
        """,
    ),
    _hub(
        "lunch",
        "Vegan Lunch Recipes",
        "Lunch",
        ["lunch"],
        """
        Vegan lunches with some substance: deli sandwiches piled with seitan pastrami, a
        lemongrass tofu banh mi, falafel with tahini sauce, jackfruit carnitas tacos, and
        chickpea and tofu salads that keep well in a lunchbox. Many of them pack well for
        the next day, and most take less effort than the photo suggests. If none of these
        fit, describe the lunch you want on the generator and get a new recipe in seconds.
        Each recipe has the full ingredient list and method step by step, while cards show a
        photo when one is available, with no life story before the ingredients.
        """,
    ),
    _hub(
        "dinner",
        "Vegan Dinner Recipes",
        "Dinner",
        ["dinner", "main", "main course", "main dish", "entree"],
        """
        Vegan dinner ideas for nights when you want a proper meal: baked ziti and stuffed
        shells, spaghetti and meatballs, pizza, burgers, street-style tofu tacos, enchiladas
        in salsa verde, orange seitan with rice, and chicken-fried steak with country gravy.
        Plenty of these are the dishes people assume they would have to give up, rebuilt
        from lentils, tofu, seitan and beans. Each recipe shows prep and cook times and
        servings, so you can pick something that fits the evening, and cards show a
        finished-plate photo when one is available.
        """,
    ),
    _hub(
        "comfort-food",
        "Vegan Comfort Food Recipes",
        "Comfort food",
        ["comfort food"],
        """
        Vegan comfort food, the heavy kind: country-fried steak under white pepper gravy,
        fried seitan with mashed potatoes, biscuits and gravy, corn dogs, blooming onions,
        fish and chips, double cheeseburgers, baked ziti, sloppy joes and cornbread. These
        are the diner, pub and county-fair classics, made without meat, dairy or eggs and
        without pretending to be health food. Each recipe has measured ingredients and a
        step-by-step method, while cards show a photo when one is available, and you can
        save any of them to your own cookbook for the next cold night.
        """,
    ),
    _hub(
        "pasta",
        "Vegan Pasta and Italian Recipes",
        "Pasta",
        ["pasta", "italian"],
        """
        Vegan pasta bakes and Italian favourites: baked ziti, stuffed shells with tofu
        ricotta, spaghetti and meatballs, penne with tomato and cannellini beans, gnocchi,
        and margherita and supreme pizzas. The creamy, cheesy parts come from cashews, tofu
        and good olive oil rather than dairy, and the sauces are made as part of the recipe.
        Recipe cards show a photo when one is available, alongside a complete ingredient
        list and the method laid out step by step, so you can get dinner on the table
        without reading through anyone's trip to Tuscany.
        """,
    ),
    _hub(
        "mexican",
        "Vegan Mexican Recipes and Tacos",
        "Mexican",
        ["mexican", "tacos", "taco", "tex mex"],
        """
        Vegan tacos and Mexican-inspired dishes: jackfruit carnitas with avocado crema,
        street-style tofu tacos, lentil and tofu burritos, enchiladas con papas in salsa
        verde, huevos rancheros, homemade flour tortillas, and even cinnamon-sugar dessert
        tacos. The fillings lean on beans, lentils, tofu and jackfruit, with cashew cheese
        and fresh salsas doing what dairy usually would. Recipe cards show a finished-dish
        photo when one is available, alongside measured ingredients and a clear method for
        taco night. The chiles and spices are all in the ingredient list, so you can turn
        the heat up or down to suit the table.
        """,
    ),
    _hub(
        "sandwiches",
        "Vegan Sandwich Recipes",
        "Sandwiches",
        ["sandwich", "sandwiches"],
        """
        Vegan sandwiches built like the deli classics: seitan pastrami on rye, a Reuben with
        house-cured seitan, portobello pastrami on sourdough, a maple-smoked tempeh BLT,
        club sandwiches, a crispy fried tofu sandwich, a lemongrass tofu banh mi and tofu
        egg-salad sandwiches. Some are quick lunches and some, like the cured seitan for the
        Reuben, are weekend projects that pay off for days. Each recipe lists everything
        that goes between the bread, with the method step by step; cards show a
        finished-sandwich photo when one is available, so you know how tall to stack it.
        """,
    ),
    _hub(
        "tofu",
        "Vegan Tofu Recipes",
        "Tofu",
        ["tofu"],
        """
        Tofu recipes that show how much range one block has: crisp fried tofu sandwiches,
        lemongrass tofu for banh mi, street-style tofu tacos, a tofu scramble for huevos
        rancheros and breakfast burritos, eggless tofu salad, and battered tofu standing in
        for fish and chips. Each recipe lists the tofu it needs alongside the other
        ingredients and walks through the method step by step. Cards show a finished-dish
        photo when one is available, and every recipe is fully vegan for lunch, dinner or
        breakfast.
        """,
    ),
    _hub(
        "high-protein",
        "High-Protein Vegan Recipes",
        "High-protein",
        ["high protein", "protein rich", "protein packed", "high in protein"],
        """
        AI-generated high-protein vegan recipes built on seitan, tofu, tempeh and lentils:
        chicken-fried seitan steak, lentil sloppy joes, lentil and tofu tacos and burritos,
        a loaded breakfast burrito, and a full English breakfast. They are here because the
        protein comes from the main ingredients rather than a scoop of powder. Recipes list
        servings and quantities so you can work out portions for your own goals; nutrition
        figures are not calculated, so check the labels on the products you use if you are
        tracking closely.
        """,
    ),
    _hub(
        "gluten-free",
        "Gluten-Free Vegan Recipes",
        "Gluten-free",
        ["gluten free"],
        """
        Vegan recipes tagged gluten-free: tacos on corn tortillas, enchiladas, huevos
        rancheros, polenta fries, cornbread, raw fruit tarts and sorbets, smoothies and
        drinks. They avoid wheat, barley and rye in the ingredient list, but they are
        AI-generated recipes and not certified, so if you cook for coeliac disease or a
        serious intolerance, check every packaged ingredient (oats, sauces and spice blends
        especially) for gluten and cross-contamination warnings. Recipe cards show a photo
        when one is available, alongside measured ingredients and a step-by-step method, and
        any of them can be saved to your cookbook.
        """,
    ),
    _hub(
        "dessert",
        "Vegan Dessert Recipes",
        "Dessert",
        ["dessert", "desserts"],
        """
        Vegan desserts with nothing to apologise for: fudgy peanut butter swirl brownies,
        double chocolate chip cookies, tiramisu cake, a baked Alaska, funnel cake, air-fried
        jelly doughnuts, s'mores, and no-bake fruit tarts, sorbets and popsicles for hot
        days. None of them use eggs, dairy butter or milk, and each recipe gives exact
        quantities and a step-by-step method, from mixing bowl to cooling rack. Cards show a
        dessert photo when one is available, so you know what you are aiming for before
        preheating the oven.
        """,
    ),
    _hub(
        "snacks",
        "Vegan Snacks and Appetizers",
        "Snacks",
        ["snack", "snacks", "appetizer", "appetizers", "party food", "finger food"],
        """
        Vegan snacks and party food: zucchini poppers, blooming onions, baked onion rings,
        polenta fries, air-fryer french fries, kettle corn, Tex-Mex snack mix, hearts of
        palm ceviche, fresh peach salsa, avocado toast and mini club sandwiches. Some are
        game-day fried snacks and some are light enough for an afternoon tea. Recipe cards
        show a photo when one is available, alongside measured ingredients and a
        step-by-step method, and most scale up easily when you are feeding a crowd. Double
        the quantities, keep the method, and put out more napkins.
        """,
    ),
)

HUBS_BY_SLUG: dict[str, TagHub] = {hub.slug: hub for hub in TAG_HUBS}


def hubs_for_tags(tags: Iterable[object]) -> list[TagHub]:
    """Hubs a recipe with ``tags`` belongs to, in ``TAG_HUBS`` order."""
    normalized = {normalize_tag(tag) for tag in tags if isinstance(tag, str)}
    return [hub for hub in TAG_HUBS if hub.aliases & normalized]
