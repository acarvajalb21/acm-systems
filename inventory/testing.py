"""Utilidades comunes de las pruebas automáticas."""
import secrets


def new_password():
    """Contraseña aleatoria que cumple los validadores (larga, con mayúscula, minúscula, dígito y símbolo)."""
    return 'Aa1-' + secrets.token_urlsafe(18)
