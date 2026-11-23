from datetime import date

from lxml import html as lh

from . import diary, food_logging
from .entry_matching import DiaryLookupError, collapse_whitespace
from .mfp_web import form_headers, get_json, require_status, site_url

# The add_to_diary form has two submit buttons, name="add" and name="delete",
# so MFP picks the action from which field is present. The value is the label
# the English page itself submits, so it matches the page whatever MFP does
# with it; the /fr/ page submits a French label to the same action.
ADD_BUTTON_VALUE = "Add Checked"

# The My Meals page (meal/mine) reads this JSON route with limit=100; its
# meal_id is the food_id that load_meals lists for the same meal.
MEALS_API_PATH = "api/services/users/meals/mine"
MEALS_API_LIMIT = "100"
INGREDIENT_NUTRIENTS = ("calories", "protein", "carbs", "fat", "sodium", "sugar")


class UnknownSavedMeal(DiaryLookupError):
    pass


class AmbiguousSavedMeal(DiaryLookupError):
    pass


def _meal_names(meals: list[dict]) -> str:
    return "; ".join(repr(meal["name"]) for meal in meals) or "(no saved meals)"


def _weight_id(row) -> str | None:
    options = row.xpath(".//select[contains(@name, '[weight_id]')]/option")
    chosen = [option for option in options if option.get("selected")] or options
    if chosen:
        return chosen[0].get("value")
    hidden = row.xpath(".//input[contains(@name, '[weight_id]')]/@value")
    return hidden[0] if hidden else None


def _row_name(row) -> str:
    cells = row.xpath("./td")
    return collapse_whitespace(cells[1].text_content()) if len(cells) > 1 else ""


def parse_saved_meals(fragment: str) -> list[dict]:
    if not fragment.strip():
        return []
    doc = lh.fromstring(f"<div>{fragment}</div>")
    meals = []
    for row in doc.xpath("//tr[contains(@class, 'favorite')]"):
        food_ids = row.xpath(".//input[contains(@name, '[food_id]')]/@value")
        weight_id = _weight_id(row)
        if food_ids and weight_id:
            meals.append(
                {"name": _row_name(row), "food_id": food_ids[0], "weight_id": weight_id}
            )
    return meals


def _add_to_diary_url(client, meal_id: str) -> str:
    return site_url(client, "food/add_to_diary", params={"meal": meal_id})


# The add_to_diary page loads its Meals tab with this POST, numbering the rows
# from base_index (the rows already on the page); 0 numbers them from 0.
def _load_saved_meals(client, csrf: str, meal_id: str) -> list[dict]:
    resp = client.session.post(
        site_url(client, "food/load_meals"),
        data={"meal": meal_id, "page": "1", "base_index": "0"},
        headers=form_headers(
            client, csrf, _add_to_diary_url(client, meal_id), accept="text/html"
        ),
    )
    require_status(resp, "/food/load_meals", (200,))
    return parse_saved_meals(resp.text)


def saved_meals(client) -> list[dict]:
    _, csrf = diary.diary_page(client, date.today())
    return _load_saved_meals(client, csrf, "0")


def find_saved_meal(meals: list[dict], name: str) -> dict:
    wanted = collapse_whitespace(name).lower()
    partial = [
        meal for meal in meals if wanted in collapse_whitespace(meal["name"]).lower()
    ]
    exact = [
        meal for meal in partial if collapse_whitespace(meal["name"]).lower() == wanted
    ]
    if exact:
        return exact[0]
    if len(partial) == 1:
        return partial[0]
    if partial:
        raise AmbiguousSavedMeal(
            f"'{name}' matches several saved meals: {_meal_names(partial)}. "
            "Pass the full name of one of them."
        )
    raise UnknownSavedMeal(
        f"no saved MyFitnessPal meal matching '{name}'. Saved meals: "
        f"{_meal_names(meals)}. To log individual foods instead, use "
        "fitness_log_food."
    )


def _post_add_favorite(client, csrf: str, day: date, meal_id: str, saved: dict) -> None:
    resp = client.session.post(
        site_url(client, "food/add_favorites"),
        data={
            "meal": meal_id,
            "date": day.isoformat(),
            "add": ADD_BUTTON_VALUE,
            "favorites[0][food_id]": saved["food_id"],
            "favorites[0][weight_id]": saved["weight_id"],
            "favorites[0][quantity]": "1",
            "favorites[0][checked]": "1",
        },
        headers=form_headers(client, csrf, _add_to_diary_url(client, meal_id)),
    )
    require_status(resp, "/food/add_favorites", (200, 204, 302))


def log_saved_meal(client, day: date, name: str, meal: str) -> dict:
    before_doc, csrf = diary.diary_page(client, day)
    meal_id, meal_label = diary.resolve_meal(before_doc, meal)
    saved = find_saved_meal(_load_saved_meals(client, csrf, meal_id), name)
    _post_add_favorite(client, csrf, day, meal_id, saved)
    added = food_logging.read_back_added(
        client, day, before_doc, what=saved["name"], meal=meal_label
    )
    if not added:
        raise food_logging.NothingLogged(
            f"MyFitnessPal accepted the saved meal {saved['name']!r} "
            f"(food_id {saved['food_id']}) but added no entry to {meal_label} on "
            f"{day.isoformat()}. Check the name with fitness_list_meals and log "
            "it again, or log its foods one by one with fitness_log_food.",
            day,
        )
    return {
        "logged": saved["name"],
        "added_entries": added,
        "food_id": saved["food_id"],
        "weight_id": saved["weight_id"],
        "meal": meal_label,
        "date": day.isoformat(),
    }


def _ingredient(food: dict) -> dict:
    return {
        "name": collapse_whitespace(food.get("description") or ""),
        **{nutrient: food.get(nutrient) for nutrient in INGREDIENT_NUTRIENTS},
    }


def _totals(ingredients: list[dict]) -> dict[str, float]:
    return {
        nutrient: round(
            sum(item[nutrient] for item in ingredients if item[nutrient] is not None),
            1,
        )
        for nutrient in INGREDIENT_NUTRIENTS
    }


def saved_meal_contents(client) -> list[dict]:
    url = site_url(
        client, MEALS_API_PATH, params={"limit": MEALS_API_LIMIT, "search": ""}
    )
    return [
        {
            "name": collapse_whitespace(item.get("description") or ""),
            "meal_id": str(item.get("meal_id")),
            "ingredients": [_ingredient(food) for food in item.get("foods") or []],
        }
        for item in get_json(client, url) or []
    ]


def meal_ingredients(client, name: str) -> dict:
    saved = find_saved_meal(saved_meal_contents(client), name)
    return {**saved, "totals": _totals(saved["ingredients"])}
