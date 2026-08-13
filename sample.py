import uuid

value = ""
try:
    uuid.UUID(str(value))
except ValueError:
    print("bad value")