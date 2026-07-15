from services.gcs_service import _object_name_from_uri, _validated_object_name_from_uri


def test_versioned_gcs_uri_resolves_for_the_same_recipe():
    assert (
        _object_name_from_uri(
            "recipe-images",
            "recipe-1",
            "gs://recipe-images/images/recipe-1/lease-token.png",
        )
        == "images/recipe-1/lease-token.png"
    )


def test_gcs_uri_cannot_select_another_recipe_object():
    assert (
        _object_name_from_uri(
            "recipe-images",
            "recipe-1",
            "gs://recipe-images/images/recipe-2/lease-token.png",
        )
        == "images/recipe-1.png"
    )
    assert (
        _validated_object_name_from_uri(
            "recipe-images",
            "recipe-1",
            "gs://recipe-images/images/recipe-2/lease-token.png",
        )
        is None
    )
