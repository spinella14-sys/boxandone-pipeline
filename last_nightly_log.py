import boto3
env = dict(l.strip().split("=", 1) for l in open("/Users/adamspinella/boxandone/.env") if "=" in l and not l.startswith("#"))
env = {k: v.strip('"\'') for k, v in env.items()}
s3 = boto3.client("s3", endpoint_url=env["R2_ENDPOINT_URL"], aws_access_key_id=env["R2_ACCESS_KEY_ID"],
                  aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"], region_name="auto")
keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=env["R2_BUCKET_NAME"], Prefix="v2/logs/nightly/")["Contents"])
print("\n".join(k.split("/")[-1] for k in keys[-3:]), "\n" + "=" * 60)
print(s3.get_object(Bucket=env["R2_BUCKET_NAME"], Key=keys[-1])["Body"].read().decode())
