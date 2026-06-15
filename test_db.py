import motor.motor_asyncio
import asyncio
import sys
import certifi

async def test_connection():
    uri = "mongodb+srv://huri_db:TTIChIpZt6F14rZf@coupon.8mp1wfm.mongodb.net/shein_bot?appName=Coupon"
    print(f"Testing URI: {uri}")
    print(f"Python: {sys.version}")
    
    options = [
        {"name": "Standard", "params": {}},
        {"name": "No SSL Verify", "params": {"tlsAllowInvalidCertificates": True, "tlsAllowInvalidHostnames": True}},
        {"name": "Certifi", "params": {"tlsCAFile": certifi.where()}},
    ]
    
    for opt in options:
        print(f"\n--- Strategy: {opt['name']} ---")
        try:
            client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000, **opt['params'])
            await client.admin.command('ping')
            print(f"✅ SUCCESS with {opt['name']}")
            return
        except Exception as e:
            print(f"❌ FAILED with {opt['name']}: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
