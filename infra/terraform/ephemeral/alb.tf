# The Application Load Balancer (D-120): internet-facing, HTTP only, reachable only from CloudFront.
# Lowest cost: no access logs, no deletion protection (a demo must be destroyable), no WAF.

resource "aws_lb" "main" {
  name               = var.name_prefix
  load_balancer_type = "application"
  internal           = false
  ip_address_type    = "ipv4"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  enable_deletion_protection = false
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "api" {
  name        = "${var.name_prefix}-api"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # awsvpc tasks register by IP
  protocol    = "HTTP"
  port        = var.api_port

  # A short drain: a destroy or a deploy does not wait the default five minutes.
  deregistration_delay = 30

  health_check {
    # /health answers while the process is alive; /ready also needs the database, and a database
    # blip must not make ECS replace healthy API tasks.
    path                = "/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# Default: refuse. Only a request that CloudFront sent with the Host of OUR distribution is
# forwarded. The security group already admits only CloudFront servers, but those serve every
# customer's distribution; this rule keeps another distribution from using this origin, without a
# secret header (its value would land in the Terraform state, D-122). With the managed
# AllViewer origin request policy CloudFront passes the viewer's Host unchanged.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = var.alb_listener_port
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "forbidden"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "from_our_cloudfront" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    host_header {
      values = [aws_cloudfront_distribution.main.domain_name]
    }
  }
}
