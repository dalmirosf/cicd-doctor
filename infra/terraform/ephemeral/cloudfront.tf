# The public HTTPS entry (D-120): a CloudFront distribution with its default *.cloudfront.net
# certificate in front of the ALB. There is no domain, so no ACM certificate. GitHub posts the
# webhook to https://<domain>/webhooks/github; the hop from CloudFront to the ALB is plain HTTP
# inside AWS, and every webhook is HMAC-signed (the app verifies X-Hub-Signature-256).
#
# CloudFront takes several minutes to deploy on `apply` and to disable on `destroy`.

# Managed policies, found by name (no ids in code).
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  name = "Managed-AllViewer" # every header (the signature and X-GitHub-*), cookie and query string
}

resource "aws_cloudfront_distribution" "main" {
  enabled         = true
  comment         = "${var.name_prefix} webhook entry"
  is_ipv6_enabled = true
  http_version    = "http2"
  price_class     = "PriceClass_100" # North America and Europe edges: the cheapest

  origin {
    origin_id   = "alb"
    domain_name = aws_lb.main.dns_name

    custom_origin_config {
      http_port                = var.alb_listener_port
      https_port               = 443
      origin_protocol_policy   = "http-only" # D-120
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 30
      origin_keepalive_timeout = 5
    }
  }

  # One behavior for everything: the API is not cacheable (webhooks are POSTs, /ready is live).
  default_cache_behavior {
    target_origin_id         = "alb"
    viewer_protocol_policy   = "https-only" # a plain-HTTP POST is refused, not redirected
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer.id
    compress                 = false
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}
