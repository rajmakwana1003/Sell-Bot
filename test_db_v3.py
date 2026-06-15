import motor.motor_asyncio
import asyncio
import sys

async def test_connection():
    # Use direct IPs
    uri = "mongodb://huri_db:TTIChIpZt6F14rZf@159.41.192.45:27017,159.41.192.66:27017,159.41.192.86:27017/shein_bot?ssl=true&replicaSet=atlas-m0-shard-0&authSource=admin&appName=Coupon"
    
    print(f"Testing IP URI: {uri}")
    
    try:
        client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000, tlsAllowInvalidCertificates=True)
        await client.admin.command('ping')
        print(f"✅ SUCCESS with IP URI!")
    except Exception as e:
        print(f"❌ FAILED with IP URI: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
