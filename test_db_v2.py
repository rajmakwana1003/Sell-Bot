import motor.motor_asyncio
import asyncio
import sys

async def test_connection():
    # Use the non-SRV connection string (Standard Connection String)
    # Reconstructed from SRV lookups and Atlas patterns
    uri = "mongodb://huri_db:TTIChIpZt6F14rZf@ac-rhoueuf-shard-00-00.8mp1wfm.mongodb.net:27017,ac-rhoueuf-shard-00-01.8mp1wfm.mongodb.net:27017,ac-rhoueuf-shard-00-02.8mp1wfm.mongodb.net:27017/shein_bot?ssl=true&replicaSet=atlas-m0-shard-0&authSource=admin&appName=Coupon"
    
    print(f"Testing Standard URI: {uri}")
    
    try:
        client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000, tlsAllowInvalidCertificates=True)
        await client.admin.command('ping')
        print(f"✅ SUCCESS with Standard URI!")
    except Exception as e:
        print(f"❌ FAILED with Standard URI: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
