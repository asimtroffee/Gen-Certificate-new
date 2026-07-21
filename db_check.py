import asyncio
from backend.supabase_init import get_client

client = get_client()
res = client.table("config").select("*").execute()
print(res.data)
