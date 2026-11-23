import asyncio
import datetime

import pytest
from conftest import FIXTURES, FakeClient, FakeResponse, diary_after_add

from myfitnesspal_mcp import food_logging, meals, mfp_client, server
from myfitnesspal_mcp.store import Store

TODAY = datetime.date(2026, 7, 8)

MEAL_ROWS = (
    '<tr><td><a data-food-entry-id="m{n}a">Rice, 100 g</a></td></tr>\n  '
    '<tr><td><a data-food-entry-id="m{n}b">Beans, 50 g</a></td></tr>'
)


@pytest.fixture
def load_meals_html():
    return (FIXTURES / "load_meals.html").read_text(encoding="utf-8")


@pytest.fixture
def meals_client(diary_html, load_meals_html):
    fake = FakeClient()
    fake.session.route(
        "GET", "food/diary?date=", diary_after_add(diary_html, MEAL_ROWS)
    )
    fake.session.route("POST", "food/load_meals", FakeResponse(text=load_meals_html))
    fake.session.route("POST", "food/add_favorites", FakeResponse(status_code=200))
    return fake


def _posts(client, fragment):
    return [
        kwargs
        for method, url, kwargs in client.session.calls
        if method == "POST" and fragment in url
    ]


def test_parse_saved_meals_reads_name_and_ids(load_meals_html):
    assert meals.parse_saved_meals(load_meals_html) == [
        {"name": "Chili Bowl", "food_id": "900000001", "weight_id": "800000001"},
        {"name": "Overnight Oats", "food_id": "900000002", "weight_id": "800000002"},
        {"name": "Oats Deluxe", "food_id": "900000003", "weight_id": "800000003"},
        {"name": "Tuna Salad", "food_id": "900000004", "weight_id": "800000004"},
    ]


def test_parse_saved_meals_of_an_empty_response():
    assert meals.parse_saved_meals("  ") == []


def test_saved_meals_posts_load_meals_from_the_first_row(meals_client):
    listed = meals.saved_meals(meals_client)
    assert [meal["name"] for meal in listed][:2] == ["Chili Bowl", "Overnight Oats"]
    [load] = _posts(meals_client, "food/load_meals")
    assert load["data"] == {"meal": "0", "page": "1", "base_index": "0"}
    assert load["headers"]["X-CSRF-Token"]
    assert _posts(meals_client, "food/add_favorites") == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("chili bowl", "Chili Bowl"),
        ("  TUNA   salad ", "Tuna Salad"),
        ("oats deluxe", "Oats Deluxe"),
        ("chili", "Chili Bowl"),
        ("overnight", "Overnight Oats"),
    ],
)
def test_find_saved_meal_exact_then_single_partial(load_meals_html, query, expected):
    found = meals.find_saved_meal(meals.parse_saved_meals(load_meals_html), query)
    assert found["name"] == expected


def test_find_saved_meal_ambiguous_lists_the_candidates(load_meals_html):
    saved = meals.parse_saved_meals(load_meals_html)
    with pytest.raises(meals.AmbiguousSavedMeal) as raised:
        meals.find_saved_meal(saved, "oats")
    message = str(raised.value)
    assert "'Overnight Oats'" in message and "'Oats Deluxe'" in message
    assert "Chili" not in message


def test_find_saved_meal_unknown_lists_every_saved_meal(load_meals_html):
    saved = meals.parse_saved_meals(load_meals_html)
    with pytest.raises(meals.UnknownSavedMeal) as raised:
        meals.find_saved_meal(saved, "lasagna session")
    assert "'Chili Bowl'" in str(raised.value)
    assert "'Tuna Salad'" in str(raised.value)
    assert not mfp_client.is_auth_error(raised.value)


def test_log_saved_meal_posts_one_checked_row(meals_client):
    result = meals.log_saved_meal(meals_client, TODAY, "tuna", "snacks")
    [add] = _posts(meals_client, "food/add_favorites")
    assert add["data"] == {
        "meal": "3",
        "date": "2026-07-08",
        "add": meals.ADD_BUTTON_VALUE,
        "favorites[0][food_id]": "900000004",
        "favorites[0][weight_id]": "800000004",
        "favorites[0][quantity]": "1",
        "favorites[0][checked]": "1",
    }
    assert _posts(meals_client, "food/load_meals")[0]["data"]["meal"] == "3"
    assert result["logged"] == "Tuna Salad"
    assert result["meal"] == "Snacks"
    assert [entry["name"] for entry in result["added_entries"]] == [
        "Rice, 100 g",
        "Beans, 50 g",
    ]


def test_log_saved_meal_with_unknown_name_adds_nothing(meals_client):
    with pytest.raises(meals.UnknownSavedMeal):
        meals.log_saved_meal(meals_client, TODAY, "lasagna", "lunch")
    assert _posts(meals_client, "food/add_favorites") == []


def test_log_saved_meal_raises_when_nothing_is_added(meals_client, diary_html):
    meals_client.session.route("GET", "food/diary?date=", FakeResponse(text=diary_html))
    with pytest.raises(food_logging.NothingLogged, match="added no entry") as raised:
        meals.log_saved_meal(meals_client, TODAY, "Chili Bowl", "dinner")
    assert "fitness_list_meals" in str(raised.value)
    assert raised.value.day == TODAY


@pytest.fixture
def connected_meals(meals_client, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_store", Store(tmp_path / "test.db"))
    monkeypatch.setattr(server.mfp_client, "get_client", lambda: meals_client)
    monkeypatch.setattr(server.sync, "refresh_day", lambda store, client, day: None)
    return meals_client


def test_list_meals_tool(connected_meals):
    result = asyncio.run(server.fitness_list_meals())
    assert result["meals"][0]["name"] == "Chili Bowl"


def test_log_meal_tool_defaults_to_lunch(connected_meals):
    result = asyncio.run(server.fitness_log_meal(name="chili", date="2026-07-08"))
    assert result["ok"] is True
    assert result["meal"] == "Lunch"
    assert len(result["added_entries"]) == 2
    [add] = _posts(connected_meals, "food/add_favorites")
    assert add["data"]["meal"] == "1"


def test_log_meal_tool_is_a_non_destructive_add():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["fitness_list_meals"].annotations.read_only_hint
    log_meal = tools["fitness_log_meal"].annotations
    assert not log_meal.read_only_hint
    assert not log_meal.destructive_hint
    assert not log_meal.idempotent_hint
