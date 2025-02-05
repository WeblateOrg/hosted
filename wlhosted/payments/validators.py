from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from vies.types import VATIN


def cache_vies_data(value):
    if isinstance(value, str):
        value = VATIN.from_str(value)
    key = f"VAT-{value}"
    data = cache.get(key)
    if data is None:
        try:
            value.verify_country_code()
            value.verify_regex()
        except ValidationError:
            return value
        data = {}
        for item in value.data:
            data[item] = value.data[item]
        cache.set(key, data, 3600)
    value.__dict__["vies_data"] = data

    return value


def validate_vatin(value) -> None:
    value = cache_vies_data(value)
    try:
        value.verify_country_code()
    except ValidationError as error:
        msg = _("{} is not a valid country code for any European Union member.")
        raise ValidationError(msg.format(value.country_code)) from error
    try:
        value.verify_regex()
    except ValidationError as error:
        msg = _("{} does not match the country's VAT ID specifications.")
        raise ValidationError(msg.format(value)) from error

    if not value.vies_data["valid"]:
        retry_errors = {"MS_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ", "TIMEOUT"}
        if value.vies_data.get("fault_reason") in retry_errors:
            msg = _(
                "VAT ID validation service unavailable for {}, please try again later."
            )
        else:
            msg = _("{} is not a valid VAT ID.")
        raise ValidationError(msg.format(value))
