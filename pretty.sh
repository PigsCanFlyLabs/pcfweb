set -ex
find \( -name 'disclosures.css' -o -name 'faq-section.css' -o -name 'lightbox.css' -o -name 'product_reviews.css' \) -exec sh -c 'cssbeautify-cli -w $1.back -f $1; mv $1.back $1' _ {} \;
