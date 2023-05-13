import secrets

from django.contrib.auth.models import User


def generate_username(email: str):
    username = email.split('@')[0]
    while User.objects.filter(username=username).exists():
        username += secrets.token_hex(8)
    return username
